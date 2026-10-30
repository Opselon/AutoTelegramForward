import java.util.Properties
import java.io.FileInputStream

plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

val keystoreProperties = Properties()
val keystorePropertiesFile = rootProject.file("key.properties")
if (keystorePropertiesFile.exists()) {
    keystoreProperties.load(FileInputStream(keystorePropertiesFile))
} else {
    // CI secret path: release keystore provided via environment variables.
    // Absent keystore => release build falls back to debug signing keys
    // (local dev only; Play Store uploads require a real keystore).
    val envStorePath = System.getenv("ANDROID_KEYSTORE_PATH")
    if (!envStorePath.isNullOrEmpty()) {
        keystoreProperties["storeFile"] = envStorePath
        keystoreProperties["storePassword"] = System.getenv("ANDROID_KEYSTORE_PASSWORD") ?: ""
        keystoreProperties["keyAlias"] = System.getenv("ANDROID_KEY_ALIAS") ?: ""
        keystoreProperties["keyPassword"] = System.getenv("ANDROID_KEY_PASSWORD") ?: ""
    }
}

android {
    namespace = "com.opselon.autoforward_mobile"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        applicationId = "com.opselon.autoforward_mobile"
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        create("release") {
            val storePath = keystoreProperties.getProperty("storeFile")
            if (!storePath.isNullOrEmpty()) {
                val resolvedFile = rootProject.file(storePath)
                if (resolvedFile.exists()) {
                    storeFile = resolvedFile
                    storePassword = keystoreProperties.getProperty("storePassword")
                    keyAlias = keystoreProperties.getProperty("keyAlias")
                    keyPassword = keystoreProperties.getProperty("keyPassword")
                }
            }
        }
    }

    buildTypes {
        release {
            val releaseConfig = signingConfigs.getByName("release")
            val isReleaseTask = gradle.startParameter.taskNames.any {
                it.contains("release", ignoreCase = true)
            }
            val allowDebugSigning =
                providers.gradleProperty("allowDebugReleaseSigning").orNull == "true"
            signingConfig = if (releaseConfig.storeFile != null) {
                releaseConfig
            } else if (allowDebugSigning || !isReleaseTask) {
                signingConfigs.getByName("debug")
            } else {
                throw GradleException(
                    "Release signing keystore is missing. Configure ANDROID_KEYSTORE_PATH and signing secrets."
                )
            }
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
