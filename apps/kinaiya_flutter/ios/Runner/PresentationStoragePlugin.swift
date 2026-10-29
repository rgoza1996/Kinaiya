import Darwin
import Flutter
import Foundation

/// The standard MethodChannel wire format, with codec and file work off the UI thread.
final class PresentationStoragePlugin: NSObject, FlutterPlugin {
  static let pluginKey = "KinaiyaPresentationStoragePlugin"
  private static let channel = "dev.kinaiya/presentation_storage_v1"
  private let messenger: FlutterBinaryMessenger
  private let attachment = PresentationStorageAttachment()
  private var connection: FlutterBinaryMessengerConnection = 0

  private init(messenger: FlutterBinaryMessenger) {
    self.messenger = messenger
    super.init()
  }

  static func register(with registrar: FlutterPluginRegistrar) {
    let instance = PresentationStoragePlugin(messenger: registrar.messenger())
    // Keep even fixed error-envelope encoding and incoming Data bridging off UI.
    guard let taskQueue = instance.messenger.makeBackgroundTaskQueue?(),
          let connection = instance.messenger.setMessageHandlerOnChannel?(
            channel, binaryMessageHandler: { [weak instance] message, reply in
              instance?.receive(message, reply: reply)
            }, taskQueue: taskQueue) else { return }
    instance.connection = connection
    // Flutter sends detach callbacks only to published plugin instances.
    registrar.publish(instance)
  }

  private func receive(_ message: Data?, reply: @escaping FlutterBinaryReply) {
    let token = attachment.token
    guard attachment.isCurrent(token) else { return }
    // The small allowance is codec framing, never an increase to the snapshot limit.
    guard let message = message,
          message.count <= PresentationStorageWorker.limit + 256 else {
      attachment.deliver(PresentationStorageFailure.invalid.envelope, token: token, reply: reply)
      return
    }
    guard let accepted = attachment.admit(token, operation: {
      PresentationStorageWorker.shared.submit(message) { [attachment] response in
        attachment.deliver(response, token: token, reply: reply)
      }
    }) else { return }
    if !accepted {
      attachment.deliver(PresentationStorageFailure.busy.envelope, token: token, reply: reply)
    }
  }

  func detachFromEngine(for registrar: FlutterPluginRegistrar) {
    attachment.retire()
    let messenger = self.messenger
    let ownedConnection = connection
    connection = 0
    // Connection-specific cleanup cannot remove a replacement handler.
    let cleanup = { messenger.cleanUpConnection(ownedConnection) }
    if Thread.isMainThread { cleanup() } else { DispatchQueue.main.async(execute: cleanup) }
  }
}

private final class PresentationStorageAttachment {
  let token = UUID()
  private let lock = NSLock()
  private var active = true

  func isCurrent(_ expected: UUID) -> Bool {
    lock.lock()
    defer { lock.unlock() }
    return active && token == expected
  }

  func retire() {
    lock.lock()
    active = false
    lock.unlock()
  }

  func admit(_ expected: UUID, operation: () -> Bool) -> Bool? {
    lock.lock()
    defer { lock.unlock() }
    guard active && token == expected else { return nil }
    return operation()
  }

  func deliver(_ response: Data?, token: UUID, reply: @escaping FlutterBinaryReply) {
    let send = { [self] in
      lock.lock()
      defer { lock.unlock() }
      guard active && self.token == token else { return }
      // Claim delivery atomically with retirement. Flutter's binary reply posts
      // its completion; it does not synchronously reenter the plugin.
      reply(response)
    }
    if Thread.isMainThread { send() } else { DispatchQueue.main.async(execute: send) }
  }
}

private enum PresentationStorageFailure: Error {
  case invalid, unavailable, busy

  var envelope: Data {
    let code: String
    let message: String
    switch self {
    case .invalid:
      code = "presentation_invalid"
      message = "Invalid presentation storage request."
    case .unavailable:
      code = "presentation_unavailable"
      message = "Presentation storage is unavailable."
    case .busy:
      code = "presentation_busy"
      message = "Presentation storage is busy."
    }
    return FlutterStandardMethodCodec.sharedInstance().encodeErrorEnvelope(
      FlutterError(code: code, message: message, details: nil))
  }
}

/// Only the two declared StandardMethodCodec call shapes are accepted. A generic
/// decoder can allocate from an untrusted container count before it sees EOF.
private enum PresentationStorageCall {
  case read, write(Data), notImplemented

  static func decode(_ message: Data) throws -> PresentationStorageCall {
    try message.withUnsafeBytes { (bytes: UnsafeRawBufferPointer) in
      var reader = PresentationStorageCallReader(bytes: bytes)
      let nameRange = try reader.string(maxBytes: 128)
      guard let name = String(bytes: bytes[nameRange], encoding: .utf8) else {
        throw PresentationStorageFailure.invalid
      }
      switch name {
      case "read":
        guard try reader.byte() == 0, reader.offset == bytes.count else {
          throw PresentationStorageFailure.invalid
        }
        return .read
      case "write":
        guard try reader.byte() == 13, try reader.size() == 1 else {
          throw PresentationStorageFailure.invalid
        }
        let key = try reader.string(maxBytes: 8)
        guard bytes[key].elementsEqual("snapshot".utf8) else {
          throw PresentationStorageFailure.invalid
        }
        let snapshot = try reader.string(maxBytes: PresentationStorageWorker.limit)
        guard reader.offset == bytes.count else { throw PresentationStorageFailure.invalid }
        // Allocate only after size, truncation, shape and strict Unicode checks.
        return .write(Data(bytes[snapshot]))
      default:
        return .notImplemented
      }
    }
  }
}

private struct PresentationStorageCallReader {
  let bytes: UnsafeRawBufferPointer
  private(set) var offset = 0

  init(bytes: UnsafeRawBufferPointer) {
    self.bytes = bytes
  }

  mutating func byte() throws -> UInt8 {
    guard offset < bytes.count else { throw PresentationStorageFailure.invalid }
    let result = bytes[offset]
    offset += 1
    return result
  }

  mutating func size() throws -> Int {
    let marker = try byte()
    if marker < 254 { return Int(marker) }
    // StandardMessageCodec uses host-endian sizes; supported iOS CPUs are LE.
    let width = marker == 254 ? 2 : 4
    var value: UInt32 = 0
    for index in 0..<width { value |= UInt32(try byte()) << (index * 8) }
    return Int(value)
  }

  mutating func string(maxBytes: Int) throws -> Range<Int> {
    guard try byte() == 7 else { throw PresentationStorageFailure.invalid }
    let count = try size()
    guard count <= maxBytes, count <= bytes.count - offset else {
      throw PresentationStorageFailure.invalid
    }
    let range = offset..<(offset + count)
    guard validUTF8(range) else { throw PresentationStorageFailure.invalid }
    offset += count
    return range
  }

  /// Reject overlong sequences, UTF-16 surrogate encodings, truncation and values
  /// above U+10FFFF before constructing a String or copying snapshot bytes.
  private func validUTF8(_ range: Range<Int>) -> Bool {
    var index = range.lowerBound
    while index < range.upperBound {
      let first = bytes[index]
      if first < 0x80 { index += 1; continue }
      let width: Int
      let secondMin: UInt8
      let secondMax: UInt8
      switch first {
      case 0xC2...0xDF:
        width = 2; secondMin = 0x80; secondMax = 0xBF
      case 0xE0:
        width = 3; secondMin = 0xA0; secondMax = 0xBF
      case 0xED:
        width = 3; secondMin = 0x80; secondMax = 0x9F
      case 0xE1...0xEC, 0xEE...0xEF:
        width = 3; secondMin = 0x80; secondMax = 0xBF
      case 0xF0:
        width = 4; secondMin = 0x90; secondMax = 0xBF
      case 0xF1...0xF3:
        width = 4; secondMin = 0x80; secondMax = 0xBF
      case 0xF4:
        width = 4; secondMin = 0x80; secondMax = 0x8F
      default:
        return false
      }
      guard width <= range.upperBound - index,
            bytes[index + 1] >= secondMin, bytes[index + 1] <= secondMax else { return false }
      for continuation in 2..<width {
        guard (0x80...0xBF).contains(bytes[index + continuation]) else { return false }
      }
      index += width
    }
    return true
  }
}

/// One process owner survives plugin/engine recreation. All admission enqueues
/// happen under the same lock, so accepted writes cannot change FIFO order.
private final class PresentationStorageWorker {
  static let shared = PresentationStorageWorker()
  static let limit = 13 * 1024 * 1024
  private let lock = NSLock()
  private let queue = DispatchQueue(label: "dev.kinaiya.presentation-storage", qos: .utility)
  private var accepted = 0

  private init() {}

  func submit(_ message: Data, completion: @escaping (Data?) -> Void) -> Bool {
    lock.lock()
    defer { lock.unlock() }
    guard accepted < 2 else { return false }
    accepted += 1
    queue.async {
      let response: Data? = autoreleasepool { self.perform(message) }
      DispatchQueue.main.async {
        // Retain the slot through delivery/retirement, including completed reads.
        completion(response)
        self.lock.lock()
        self.accepted -= 1
        self.lock.unlock()
      }
    }
    return true
  }

  private func perform(_ message: Data) -> Data? {
    let codec = FlutterStandardMethodCodec.sharedInstance()
    do {
      switch try PresentationStorageCall.decode(message) {
      case .read:
        return codec.encodeSuccessEnvelope(try read())
      case .write(let data):
        let file = try snapshotURL()
        try validateFileIfPresent(file)
        try data.write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        return codec.encodeSuccessEnvelope(nil)
      case .notImplemented:
        return nil // Standard MethodChannel "not implemented" binary reply.
      }
    } catch let error as PresentationStorageFailure {
      return error.envelope
    } catch {
      return PresentationStorageFailure.unavailable.envelope
    }
  }

  private func snapshotURL() throws -> URL {
    let manager = FileManager.default
    let support = try manager.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                  appropriateFor: nil, create: true)
    var directory = support.appendingPathComponent("KinaiyaPresentation-v1", isDirectory: true)
    var info = stat()
    if directory.path.withCString({ lstat($0, &info) }) != 0 {
      guard errno == ENOENT else { throw PresentationStorageFailure.unavailable }
      try manager.createDirectory(at: directory, withIntermediateDirectories: false,
                                  attributes: [.posixPermissions: 0o700])
      guard directory.path.withCString({ lstat($0, &info) }) == 0 else {
        throw PresentationStorageFailure.unavailable
      }
    }
    guard info.st_mode & mode_t(S_IFMT) == mode_t(S_IFDIR), info.st_uid == geteuid() else {
      throw PresentationStorageFailure.unavailable
    }
    try manager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
    // Exclude the stable parent; atomic replacement may replace the file inode.
    var values = URLResourceValues()
    values.isExcludedFromBackup = true
    try directory.setResourceValues(values)
    return directory.appendingPathComponent("snapshot.json", isDirectory: false)
  }

  private func validateFileIfPresent(_ file: URL) throws {
    var info = stat()
    if file.path.withCString({ lstat($0, &info) }) != 0 {
      guard errno == ENOENT else { throw PresentationStorageFailure.unavailable }
      return
    }
    guard info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG), info.st_nlink == 1,
          info.st_uid == geteuid() else { throw PresentationStorageFailure.unavailable }
  }

  private func read() throws -> String? {
    let file = try snapshotURL()
    let descriptor = file.path.withCString { open($0, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK) }
    guard descriptor >= 0 else {
      if errno == ENOENT { return nil }
      throw PresentationStorageFailure.unavailable
    }
    let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
    defer { try? handle.close() }
    var info = stat()
    guard fstat(descriptor, &info) == 0,
          info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG), info.st_nlink == 1,
          info.st_uid == geteuid() else { throw PresentationStorageFailure.unavailable }
    guard info.st_size >= 0, info.st_size <= Int64(Self.limit) else {
      throw PresentationStorageFailure.invalid
    }
    var data = Data()
    data.reserveCapacity(Int(info.st_size))
    while let chunk = try handle.read(upToCount: min(64 * 1024, Self.limit - data.count + 1)),
          !chunk.isEmpty {
      guard data.count + chunk.count <= Self.limit else { throw PresentationStorageFailure.invalid }
      data.append(chunk)
    }
    guard let snapshot = String(data: data, encoding: .utf8) else {
      throw PresentationStorageFailure.invalid
    }
    return snapshot
  }
}
