import 'package:flutter/material.dart';

enum OptionsPage {
  overview('Options'),
  models('Models'),
  providers('Providers'),
  harnesses('Harness Customization'),
  voice('Voice'),
  audio('Audio'),
  agent('Agent'),
  permissions('Permissions'),
  tools('Tools'),
  integrations('Integrations'),
  memory('Memory'),
  data('Data'),
  tasks('Tasks'),
  resources('Resources'),
  appearance('Appearance'),
  character('Character'),
  connection('Connection'),
  privacy('Privacy'),
  accessibility('Accessibility'),
  about('About');

  const OptionsPage(this.label);
  final String label;
}

class OptionsCategory {
  const OptionsCategory(this.label, this.icon, this.pages);
  final String label;
  final IconData icon;
  final List<OptionsPage> pages;
}

const optionsCategories = [
  OptionsCategory('Models & providers', Icons.diamond_outlined, [
    OptionsPage.models,
    OptionsPage.providers,
    OptionsPage.harnesses,
  ]),
  OptionsCategory('Voice & audio', Icons.graphic_eq, [
    OptionsPage.voice,
    OptionsPage.audio,
  ]),
  OptionsCategory('Agent & permissions', Icons.smart_toy_outlined, [
    OptionsPage.agent,
    OptionsPage.permissions,
  ]),
  OptionsCategory('Tools & integrations', Icons.extension_outlined, [
    OptionsPage.tools,
    OptionsPage.integrations,
  ]),
  OptionsCategory('Memory & data', Icons.storage_outlined, [
    OptionsPage.memory,
    OptionsPage.data,
  ]),
  OptionsCategory('Tasks & resources', Icons.speed_outlined, [
    OptionsPage.tasks,
    OptionsPage.resources,
  ]),
  OptionsCategory('Appearance & character', Icons.auto_awesome_outlined, [
    OptionsPage.appearance,
    OptionsPage.character,
  ]),
  OptionsCategory('Connection & privacy', Icons.link_outlined, [
    OptionsPage.connection,
    OptionsPage.privacy,
  ]),
  OptionsCategory('Accessibility', Icons.accessibility_new, [
    OptionsPage.accessibility,
  ]),
  OptionsCategory('About', Icons.info_outline, [OptionsPage.about]),
];
