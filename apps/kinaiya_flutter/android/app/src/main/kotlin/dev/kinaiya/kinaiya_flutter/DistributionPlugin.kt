package dev.kinaiya.kinaiya_flutter

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

/**
 * Opens an official Kinaiya GitHub release page in the user's browser (#263).
 *
 * The URL is re-validated here, independently of Dart. Nothing is downloaded or
 * installed by the app: the browser download and Android's package installer stay
 * under user control, and Android only accepts an update signed with the same
 * certificate as the installed app.
 */
class DistributionPlugin : FlutterPlugin, MethodChannel.MethodCallHandler {
    private var channel: MethodChannel? = null
    private var context: Context? = null

    override fun onAttachedToEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        context = binding.applicationContext
        channel = MethodChannel(binding.binaryMessenger, "kinaiya/distribution").also {
            it.setMethodCallHandler(this)
        }
    }

    override fun onDetachedFromEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        channel?.setMethodCallHandler(null)
        channel = null
        context = null
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        if (call.method != "openReleasePage") {
            result.notImplemented()
            return
        }
        val url = call.arguments as? String
        val appContext = context
        if (url == null || appContext == null || !OFFICIAL_RELEASE_PAGE.matches(url)) {
            result.success(false)
            return
        }
        try {
            appContext.startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse(url))
                    .addCategory(Intent.CATEGORY_BROWSABLE)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
            result.success(true)
        } catch (error: ActivityNotFoundException) {
            result.success(false)
        }
    }

    private companion object {
        val OFFICIAL_RELEASE_PAGE =
            Regex(
                "https://github\\.com/rgoza1996/Kinaiya/releases/tag/" +
                    "(v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)|nightly-[1-9][0-9]{0,18}-[0-9a-f]{12})",
            )
    }
}
