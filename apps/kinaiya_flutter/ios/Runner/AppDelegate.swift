import Flutter
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    let registry = engineBridge.pluginRegistry
    GeneratedPluginRegistrant.register(with: registry)
    if !registry.hasPlugin(PresentationStoragePlugin.pluginKey),
       let registrar = registry.registrar(forPlugin: PresentationStoragePlugin.pluginKey) {
      PresentationStoragePlugin.register(with: registrar)
    }
    if !registry.hasPlugin(DistributionPlugin.pluginKey),
       let registrar = registry.registrar(forPlugin: DistributionPlugin.pluginKey) {
      DistributionPlugin.register(with: registrar)
    }
  }
}

/// Opens an official Kinaiya GitHub release page in the browser (#264).
///
/// The URL is re-validated here, independently of Dart. The app never downloads or
/// installs its own update; the user's sideloading tool installs the IPA.
final class DistributionPlugin: NSObject, FlutterPlugin {
  static let pluginKey = "KinaiyaDistributionPlugin"
  private static let officialReleasePage =
    #"\Ahttps://github\.com/rgoza1996/Kinaiya/releases/tag/(v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)|nightly-[1-9][0-9]{0,18}-[0-9a-f]{12})\z"#

  static func register(with registrar: FlutterPluginRegistrar) {
    let channel = FlutterMethodChannel(
      name: "kinaiya/distribution", binaryMessenger: registrar.messenger())
    let instance = DistributionPlugin()
    registrar.addMethodCallDelegate(instance, channel: channel)
  }

  func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    guard call.method == "openReleasePage" else {
      result(FlutterMethodNotImplemented)
      return
    }
    guard let raw = call.arguments as? String,
          raw.range(of: Self.officialReleasePage, options: .regularExpression) != nil,
          let url = URL(string: raw) else {
      result(false)
      return
    }
    UIApplication.shared.open(url, options: [:]) { opened in result(opened) }
  }
}
