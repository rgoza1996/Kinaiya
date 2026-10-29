package dev.kinaiya.kinaiya_flutter

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.AtomicFile
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCodec
import io.flutter.plugin.common.StandardMethodCodec
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileNotFoundException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.charset.CodingErrorAction
import java.nio.charset.StandardCharsets
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CompletableFuture
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit

/** A fixed presentation file, with bounded work retained through reply delivery. */
class PresentationStoragePlugin : FlutterPlugin {
  @Volatile private var attachment: Any? = null
  private var messenger: BinaryMessenger? = null
  private val main = Handler(Looper.getMainLooper())

  override fun onAttachedToEngine(binding: FlutterPlugin.FlutterPluginBinding) {
    val token = Any()
    attachment = token
    val binary = binding.binaryMessenger
    messenger = binary
    val context = binding.applicationContext
    // Flutter invalidates the callback ByteBuffer on handler return. Copy it
    // while valid on a background task queue, then use our process-wide worker.
    binary.setMessageHandler(CHANNEL, { message, reply ->
      val copied = CompletableFuture<ByteArray?>()
      val accepted = synchronized(admission) {
        if (pending >= 2) {
          false
        } else {
          pending++
          try {
            worker.execute {
              val envelope = try {
                val raw = copied.get()
                if (raw == null) throw InvalidSnapshot()
                execute(context, raw)
              } catch (_: InvalidSnapshot) {
                error("presentation_invalid", "Invalid presentation snapshot.")
              } catch (_: Exception) {
                error("presentation_unavailable", "Presentation storage is unavailable.")
              }
              deliver(token, reply, envelope, counted = true)
            }
            true
          } catch (_: RejectedExecutionException) {
            pending--
            false
          } catch (_: Exception) {
            pending--
            false
          }
        }
      }
      if (!accepted) {
        deliver(token, reply,
          error("presentation_busy", "Presentation storage is busy."), counted = false)
      } else {
        // Do not hold the admission lock during this copy: main-thread permit
        // release must never wait for a large conversion/copy or disk operation.
        val raw = try {
          if (message != null && message.remaining() <= MAX_BYTES + 256) {
            ByteArray(message.remaining()).also { message.get(it) }
          } else null
        } catch (_: Exception) { null }
        copied.complete(raw)
      }
    }, binary.makeBackgroundTaskQueue())
  }

  override fun onDetachedFromEngine(binding: FlutterPlugin.FlutterPluginBinding) {
    attachment = null
    messenger?.setMessageHandler(CHANNEL, null)
    messenger = null
    // Already accepted operations keep their place; only this attachment's
    // reply is retired. A later attach has a different immutable token.
  }

  private fun deliver(
    token: Any,
    reply: BinaryMessenger.BinaryReply,
    envelope: ByteBuffer?,
    counted: Boolean,
  ) {
    val posted = main.post {
      try {
        if (attachment === token) reply.reply(envelope)
      } catch (_: RuntimeException) {
        // A lost engine makes delivery unconfirmed, never a new write/retry.
      } finally {
        if (counted) release()
      }
    }
    if (!posted && counted) release()
  }

  private class InvalidSnapshot : Exception()
  private class StorageUnavailable : Exception()

  companion object {
    private const val CHANNEL = "dev.kinaiya/presentation_storage_v1"
    private const val FILE_NAME = "presentation_snapshot_v1"
    private const val MAX_BYTES = 13 * 1024 * 1024
    // Use the interface's nullable success/details contract; the implementation
    // carries narrower Java annotations despite supporting null wire values.
    private val codec: MethodCodec = StandardMethodCodec.INSTANCE
    private val admission = Any()
    private var pending = 0
    private val worker = ThreadPoolExecutor(
      1, 1, 0L, TimeUnit.MILLISECONDS, ArrayBlockingQueue<Runnable>(1),
      { task -> Thread(task, "presentation-storage").apply { isDaemon = true } },
    )

    private fun release() = synchronized(admission) { pending-- }
    private fun error(code: String, message: String): ByteBuffer =
      codec.encodeErrorEnvelope(code, message, null)

    // Parse only our narrow StandardMethodCodec call shapes. A generic decoder
    // can allocate from an untrusted declared container count before noticing a
    // short buffer. No arrays, nested maps, paths or arbitrary values are needed.
    private fun size(input: ByteBuffer): Int = when (val first = input.get().toInt() and 255) {
      254 -> input.short.toInt() and 65535
      255 -> input.int.also { if (it < 0) throw InvalidSnapshot() }
      else -> first
    }

    private fun string(input: ByteBuffer, limit: Int): String {
      if (input.get().toInt() != 7) throw InvalidSnapshot()
      val count = size(input)
      if (count > limit || count > input.remaining()) throw InvalidSnapshot()
      val slice = input.slice().apply { this.limit(count) }
      val value = StandardCharsets.UTF_8.newDecoder()
        .onMalformedInput(CodingErrorAction.REPORT)
        .onUnmappableCharacter(CodingErrorAction.REPORT).decode(slice).toString()
      input.position(input.position() + count)
      return value
    }

    private fun execute(context: Context, raw: ByteArray): ByteBuffer? {
      val input = ByteBuffer.wrap(raw).order(ByteOrder.nativeOrder())
      val method = try { string(input, 128) } catch (_: Exception) { throw InvalidSnapshot() }
      val result: String? = when (method) {
        "read" -> {
          if (input.remaining() != 1 || input.get().toInt() != 0) throw InvalidSnapshot()
          read(context)
        }
        "write" -> {
          val snapshot = try {
            if (input.get().toInt() != 13 || size(input) != 1 ||
              string(input, 8) != "snapshot") throw InvalidSnapshot()
            string(input, MAX_BYTES).also { if (input.hasRemaining()) throw InvalidSnapshot() }
          } catch (_: Exception) { throw InvalidSnapshot() }
          write(context, snapshot)
          null
        }
        else -> return null // MethodChannel notImplemented; do not decode foreign arguments.
      }
      return codec.encodeSuccessEnvelope(result)
    }

    private fun file(context: Context): File {
      val directory = context.noBackupFilesDir ?: throw StorageUnavailable()
      if (!directory.isDirectory && !directory.mkdirs()) throw StorageUnavailable()
      return File(directory, FILE_NAME)
    }

    private fun read(context: Context): String? {
      val base = file(context)
      val stream = try { AtomicFile(base).openRead() } catch (_: FileNotFoundException) {
        if (!base.exists() && !File(base.path + ".bak").exists()) return null
        throw StorageUnavailable()
      }
      val bytes = stream.use {
        if (it.channel.size() > MAX_BYTES) throw InvalidSnapshot()
        val sink = ByteArrayOutputStream(8192)
        val chunk = ByteArray(8192)
        var total = 0
        while (true) {
          val count = it.read(chunk)
          if (count < 0) break
          if (total + count > MAX_BYTES) throw InvalidSnapshot()
          sink.write(chunk, 0, count)
          total += count
        }
        sink.toByteArray()
      }
      return try {
        StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
          .onUnmappableCharacter(CodingErrorAction.REPORT)
          .decode(ByteBuffer.wrap(bytes)).toString()
      } catch (_: Exception) { throw InvalidSnapshot() }
    }

    private fun write(context: Context, snapshot: String) {
      // The narrow parser already strictly decoded at most MAX_BYTES of UTF-8.
      // Re-encoding cannot expand or replace malformed input.
      val bytes = snapshot.toByteArray(StandardCharsets.UTF_8)
      if (bytes.size > MAX_BYTES) throw InvalidSnapshot()
      val atomic = AtomicFile(file(context))
      val output = atomic.startWrite()
      try {
        output.write(bytes)
        // AtomicFile can log sync/rename failures without throwing. Surface a
        // checked sync while the old file is intact, then verify committed data.
        output.fd.sync()
        atomic.finishWrite(output)
      } catch (_: Exception) {
        atomic.failWrite(output)
        throw StorageUnavailable()
      }
      val committed = try { read(context) == snapshot } catch (_: Exception) { false }
      if (!committed) throw StorageUnavailable()
    }
  }
}
