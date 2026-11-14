#!/bin/bash
# User-local build tools; no system package changes or administrator credentials.
set -euo pipefail
test "$(id -un)" = kinaiya-ci
umask 077
mkdir -p "$HOME/.local/bin" "$HOME/tmp"
case "$(uname -m)" in
  aarch64) target=aarch64-unknown-linux-gnu; digest=befa1a59c91e96eb601b0fd9a97c03dd666f17baba644b2b4db9c59a767e387e ;;
  x86_64) target=x86_64-unknown-linux-gnu; digest=6426a73c3837e6e2483ee344cbc00f36394d179afcba6183cb77437e67db4af0 ;;
  *) exit 1 ;;
esac
stage=$(mktemp -d "$HOME/tmp/tooling.XXXXXX")
curl -fsSL --retry 3 "https://github.com/astral-sh/uv/releases/download/0.11.26/uv-$target.tar.gz" -o "$stage/uv.tar.gz"
printf '%s  %s\n' "$digest" "$stage/uv.tar.gz" | sha256sum -c -
tar -xzf "$stage/uv.tar.gz" -C "$stage"
install -m 0755 "$stage/uv-$target/uv" "$HOME/.local/bin/uv"
"$HOME/.local/bin/uv" --version
if test "$target" = x86_64-unknown-linux-gnu; then
  if ! command -v unzip >/dev/null; then
    # Download/extract the distro package into this account; do not install it
    # into Dell's full root filesystem or modify the host dpkg database.
    (cd "$stage" && apt-get download unzip=6.0-29ubuntu1)
    printf '%s  %s\n' 1f6f2bdcde84641e89c9367a7a99d90e990efa3e487a8d625241c2f9c55db5f4 "$stage/unzip_6.0-29ubuntu1_amd64.deb" | sha256sum -c -
    dpkg-deb -x "$stage/unzip_6.0-29ubuntu1_amd64.deb" "$HOME/.local/unzip-package"
    ln -s "$HOME/.local/unzip-package/usr/bin/unzip" "$HOME/.local/bin/unzip"
  fi
  mkdir -p "$HOME/.gradle"
  cat > "$HOME/.gradle/gradle.properties" <<'GRADLE'
org.gradle.jvmargs=-Xmx3g -XX:MaxMetaspaceSize=1g -XX:ReservedCodeCacheSize=256m -Dfile.encoding=UTF-8
org.gradle.workers.max=2
org.gradle.parallel=false
org.gradle.daemon=false
kotlin.compiler.execution.strategy=in-process
GRADLE
  test ! -e "$HOME/jdk17"
  mkdir "$HOME/jdk17"
  curl -fsSL --retry 3 'https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.21%2B9/OpenJDK17U-jdk_x64_linux_hotspot_17.0.21_9.tar.gz' -o "$stage/jdk.tar.gz"
  printf '%s  %s\n' 8b9b611d917e3dfd8419d3566b5949226f69b052fed7e109c1368add1dfe8711 "$stage/jdk.tar.gz" | sha256sum -c -
  tar -xzf "$stage/jdk.tar.gz" -C "$HOME/jdk17" --strip-components=1
  export JAVA_HOME="$HOME/jdk17"
  export ANDROID_HOME="$HOME/android-sdk"
  mkdir -p "$ANDROID_HOME/cmdline-tools"
  curl -fsSL --retry 3 'https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip' -o "$stage/android.zip"
  printf '%s  %s\n' 4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583 "$stage/android.zip" | sha256sum -c -
  python3 -m zipfile -e "$stage/android.zip" "$stage/android"
  mv -- "$stage/android/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"
  chmod 0755 "$ANDROID_HOME/cmdline-tools/latest/bin/"*
  # sdkmanager installs only this CI account's SDK; licenses stay in that SDK.
  set +o pipefail
  yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$ANDROID_HOME" --licenses > "$HOME/android-licenses.log" 2>&1
  license_status=${PIPESTATUS[1]}
  set -o pipefail
  test "$license_status" -eq 0
  "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$ANDROID_HOME" \
    'platform-tools' 'platforms;android-36' 'build-tools;36.0.0' 'ndk;28.2.13676358' > "$HOME/android-install.log" 2>&1
  "$JAVA_HOME/bin/java" -version
  test -f "$ANDROID_HOME/ndk/28.2.13676358/source.properties"
fi
# Delete only the unique directory just created under this account's own tmp.
test "$(dirname "$stage")" = "$HOME/tmp"
rm -rf -- "$stage"
