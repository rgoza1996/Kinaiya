import java.security.KeyStore
import java.security.cert.X509Certificate
import java.util.Base64

plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// Installed identity (#263) is derived from what Flutter actually compiles, so the
// application ID cannot disagree with the Dart entry point or the Dart-visible
// release channel:
// - entry point: Flutter's `target` property (lib/main.dart or lib/companion_main.dart)
// - channel: the KINAIYA_CHANNEL --dart-define (absent => development)
// Stable keeps the original base ID; nightly/development use suffixes so channels
// and the owner/companion apps coexist without sharing app-private state.
val flutterTarget = (findProperty("target") as String? ?: "lib/main.dart").replace('\\', '/')
val companion = flutterTarget.endsWith("lib/companion_main.dart")
val dartDefines: Map<String, String> =
    (findProperty("dart-defines") as String? ?: "")
        .split(',')
        .filter { it.isNotEmpty() }
        .mapNotNull { encoded ->
            runCatching { String(Base64.getDecoder().decode(encoded), Charsets.UTF_8) }
                .getOrNull()
                ?.split('=', limit = 2)
                ?.takeIf { it.size == 2 }
                ?.let { it[0] to it[1] }
        }
        .toMap()
val kinaiyaChannel = dartDefines["KINAIYA_CHANNEL"] ?: "development"
check(kinaiyaChannel in setOf("development", "nightly", "stable")) {
    "Unsupported KINAIYA_CHANNEL '$kinaiyaChannel'"
}
val distributionChannel = kinaiyaChannel != "development"
val idSuffix =
    (if (companion) ".companion" else "") +
        when (kinaiyaChannel) {
            "stable" -> ""
            "nightly" -> ".nightly"
            else -> ".dev"
        }
val appLabel =
    (if (companion) "Kinaiya Companion" else "Kinaiya") +
        when (kinaiyaChannel) {
            "stable" -> ""
            "nightly" -> " Nightly"
            else -> " Dev"
        }

// Distribution signing comes only from the environment of the release job. It is
// never committed and there is no fallback to the debug certificate.
fun signingEnv(name: String): String? = System.getenv(name)?.takeIf { it.isNotEmpty() }

val signingNames =
    listOf(
        "KINAIYA_ANDROID_KEYSTORE",
        "KINAIYA_ANDROID_KEYSTORE_PASSWORD",
        "KINAIYA_ANDROID_KEY_ALIAS",
        "KINAIYA_ANDROID_KEY_PASSWORD",
    )
val distributionSigningConfigured = signingNames.all { signingEnv(it) != null }

android {
    namespace = "dev.kinaiya.kinaiya_flutter"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        applicationId = "dev.kinaiya.kinaiya_flutter$idSuffix"
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        // Release jobs pass --build-number/--build-name; nightly build numbers are
        // the monotonic distribution workflow run number.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
        manifestPlaceholders["kinaiyaAppLabel"] = appLabel
    }

    signingConfigs {
        if (distributionSigningConfigured) {
            create("distribution") {
                storeFile = file(signingEnv("KINAIYA_ANDROID_KEYSTORE")!!)
                storePassword = signingEnv("KINAIYA_ANDROID_KEYSTORE_PASSWORD")
                keyAlias = signingEnv("KINAIYA_ANDROID_KEY_ALIAS")
                keyPassword = signingEnv("KINAIYA_ANDROID_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            signingConfig =
                when {
                    distributionChannel && distributionSigningConfigured ->
                        signingConfigs.getByName("distribution")
                    // Development release builds keep the template's debug signing so
                    // `flutter run --release` works; they carry the ".dev" identity.
                    !distributionChannel -> signingConfigs.getByName("debug")
                    // Distribution without signing material: verifyDistributionSigning
                    // below fails the build before anything is packaged.
                    else -> null
                }
        }
    }
}

// Fail closed at execution time (debug/development builds are unaffected): a
// nightly/stable release requires the configured distribution key, and that key
// must not be an Android debug certificate.
val verifyDistributionSigning =
    tasks.register("verifyDistributionSigning") {
        doLast {
            if (!distributionChannel) return@doLast
            val missing = signingNames.filter { signingEnv(it) == null }
            check(missing.isEmpty()) {
                "Release channel '$kinaiyaChannel' requires distribution signing; missing: $missing"
            }
            val keystore = file(signingEnv("KINAIYA_ANDROID_KEYSTORE")!!)
            check(keystore.isFile) { "Distribution keystore file is missing" }
            val store =
                KeyStore.getInstance(
                    keystore,
                    signingEnv("KINAIYA_ANDROID_KEYSTORE_PASSWORD")!!.toCharArray(),
                )
            val alias = signingEnv("KINAIYA_ANDROID_KEY_ALIAS")!!
            check(store.isKeyEntry(alias)) { "Distribution key alias is missing" }
            val certificate =
                store.getCertificate(alias) as? X509Certificate
                    ?: error("Distribution signing requires an X.509 certificate")
            certificate.checkValidity()
            check(!certificate.subjectX500Principal.name.contains("CN=Android Debug", ignoreCase = true)) {
                "An Android debug certificate is not a distribution signing identity"
            }
        }
    }
tasks.matching { it.name == "preReleaseBuild" }.configureEach { dependsOn(verifyDistributionSigning) }

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
