package dev.kinaiya.kinaiya_flutter

import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterActivity() {
  // App-private presentation storage plugin. Registered per engine so both
  // Dart entrypoints (owner and companion) share the same channel contract;
  // the plugin delegates to a process-shared serial worker, so engine or
  // activity recreation never duplicates or resets admission ordering.
  override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
    super.configureFlutterEngine(flutterEngine)
    if (!flutterEngine.plugins.has(PresentationStoragePlugin::class.java)) {
      flutterEngine.plugins.add(PresentationStoragePlugin())
    }
  }
}
