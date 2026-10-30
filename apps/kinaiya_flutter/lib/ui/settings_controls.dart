/// Presentation-only handles owned by the currently mounted panes.
///
/// They grant no runtime authority. A pane installs a guarded closure and may
/// detach only that same closure; stale holders cannot operate a replacement.
library;

class ConverseViewControls {
  Future<void> Function()? openVoiceOptions;
  Future<void> Function()? openTextConversation;
}

class AgentViewControls {
  void Function()? openCommands;
}
