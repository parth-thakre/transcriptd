# Android

The Android app (`android/`) works like Wispr Flow on Android: it doesn't replace your keyboard or take over the screen. On every text box (whenever the keyboard is up or a text field has focus), a small bubble floats at the edge of the screen. Hold it and talk; let go and the text goes in at the cursor. Tap it instead to talk hands-free: it becomes a small bar with ✕ (discard), a voice level, and ✓ (insert). Drag it to move it; it snaps to the nearest side and remembers the spot, separately for the Flip's cover screen and the main screen. It hides in password fields and when no text box is in use. It follows the screen you're using: folded, the Flip's cover screen is a display of its own (id 1) and the main one is off, so the service reads windows from every display (`windowsOnAllDisplays`), picks the one that's on and has the keyboard, and adds its overlay there through a display context. A text box counts if the accessibility tree has a focused editable field, Android has started input in a text editor (the service is an accessibility input method, `flagInputMethodEditor`), or simply the keyboard is up. The editor's input type marks password fields. When only the keyboard gives it away (a web view or custom editor that exposes neither), the bubble still shows, the text goes in through the input connection if the same app is still in front, and the app sends a `bubble_blind` report (once per app) with what had focus.

It talks to your transcriptd server directly, with the same `/v1/dictate` socket as the desktop agent. It only offers speech-to-text profiles and picks a Whisper one by default (a fixed-English one first, such as `english-whisper`). The server's LLM pass is on by default (**Polish with the LLM** in the app): it fixes punctuation, capitals and numbers on plain text and cuts ramble (um, like, you know, repeats, false starts, "at 7, actually 3", asides like "this is going to be a long one"), then the style you pick lays it out, and the guard checks the result against the raw words.

## How it's put together

| File | What it does |
| --- | --- |
| `FlowService.kt` | The accessibility service. It watches focus and window changes to decide when the bubble shows, reads the text around the cursor for context, and inserts the result |
| `Bubble.kt` | The overlay (`TYPE_ACCESSIBILITY_OVERLAY`): drawing, hold / tap / slide-off / drag handling, placement above the keyboard |
| `Dictation.kt` | One dictation: opens the socket while the mic starts, buffers audio until the server is `ready`, keeps every frame, and falls back to uploading the take to `/v1/transcribe` |
| `Recorder.kt` | `AudioRecord`, 16 kHz mono PCM16 in 20 ms frames; the source is a setting (Raw `VOICE_RECOGNITION` by default, Voice call, Standard) |
| `CaretContext.kt` | How much of the field around the caret goes with a take (at most 400 characters before, 100 after, cut at a word) |
| `EditWatch.kt` | With **Learn from my edits** on: follows the field after a dictation went in until you're done with it, and picks the window around the dictation to send |
| `ServerUrl.kt` | Which server addresses the app will use: plain `http://` only on your own network, `https://` everywhere else |
| `Telemetry.kt` | A report per take for the server's `/v1/telemetry`, queued on the phone while the server is unreachable |
| `MicService.kt` | A microphone foreground service that runs only while you dictate (Android's mic indicator, recording over other apps) |
| `Styles.kt` | "By app": chat apps get Message, mail apps Email, notes apps Note, everything else Plain |
| `MainActivity.kt` | Setup (pairing, permissions, style, speech model) and the playground |

**Inserting.** The text goes in through the accessibility input connection (`AccessibilityService.getInputMethod()`, Android 13+), which types at the cursor like a keyboard and keeps the field's formatting. If that's unavailable, the app rewrites the field's text around the selection (`ACTION_SET_TEXT`), and as a last resort it pastes, putting your clipboard back afterwards. If focus moved to another app before the result came back, the text is copied instead and a toast says so.

**What it reads and sends.** Everything goes to your own server and nowhere else.
- *With every take:* the text around the cursor, as `context`: at most 400 characters before it and 100 after, cut back to a whole word (`CaretContext.kt`). That's what the server uses: the last characters before the cursor and the first one after decide the leading space and first-letter case of `insert_text`, names in it act as vocabulary for that dictation, and a mostly Devanagari field picks a multilingual profile (the server looks at the same 400 + 100). Nothing else in the field or on screen is read for this.
- *With **Learn from my edits** on (off by default):* after the text goes in, the app keeps reading that one field until you're done with it (you sent it, moved to another field or app, or 90 seconds passed), and then posts to `/v1/dictations/{id}/edited`. Edited: `{"inserted_text": …, "final_text": …, "app": …, "reason": …}`, where `final_text` is a window of the field: the dictation as it now reads plus up to 200 characters either side, cut at words, at most 4,000 characters (in a short field that can be the whole field). Unchanged (that part of the field is still what went in): `{"final_text": null, "app": …, "reason": …}`, with no text; the server uses its own copy of what was inserted. If the dictation was never seen in the field, or can't be placed in it any more, nothing is sent, even when the field was sent. Anything beyond the window stays on the phone. Off, the field isn't read again after the insertion and nothing is posted.
- *Telemetry,* below: no text from any field.

**Not losing a take.** If the socket fails, the audio recorded so far is uploaded as a WAV once you stop. If that fails too, the bubble turns into a "Not sent" bar: retry sends the same audio again, ✕ discards it.

**The cover screen.** The app uses the same geometry as the web app (see `server/transcriptd/web/app.css`). Flip cover screen detection: a screen at most 630×700 dp that's between 3:4 and 4:3 is a Flip cover screen. On the Flip 7 (1.07+ times taller than wide) the cameras take the bottom ~22% from about 43% of the width to the right edge, and on the Flip 5/6 the bottom ~10%. Samsung's Flip 7 cover-display emulator skin agrees: its mask puts the flash and lenses at x 457–907, y 843–1017 of the 948×1048 panel (from 48% across, the bottom 19.6%), so the band has a small margin. The bubble always stays above the keyboard and that band. Its default spot is the right edge, 64 dp above the keyboard, clear of the row where apps put their send button. With the keyboard up the cover screen has only ~120 dp left, so there the bubble may ride up over the status bar rather than slide down onto the app's buttons. The setup screen's scroll area ends above the band, so no control ever sits under the cameras.

**Telemetry.** After every take, including cancelled and unsent ones, the app posts a report to `/v1/telemetry`. It covers:
- the phone and app version
- the screen (cover or main, window and panel size, every lit display with the cutout it reports)
- the network
- the microphone: the source asked for, the source and input device Android actually used, and the effects running on it
- milestones in ms from the press: mic open, first frame, socket open, server ready, released, end sent, socket failed, upload
- the path (socket or upload), the mode (hold or hands-free), the outcome and any error, and letting go → text
- the app you dictated into (its package name), the style, and whether the field was seen as an accessibility node, only through the keyboard, or not at all
- in the playground only, the sentence you were asked to read out

It never contains text from the field or what you said; the transcript is on the server already. When the keyboard is up but no text field can be seen, the app also sends one `bubble_blind` report per app: the app's package name, the class of the focused view, and the package names of the windows on screen.

The server keeps it next to its own record of the take: what the model heard, the timings, and audio stats computed on every take (speech level, noise floor, the gap between them, clipping, silence before and after the speech, and the share of energy under 120 Hz, i.e. handling or wind rumble). With `keep_dictation_audio = true` it also keeps the WAV, forever, until that dictation is deleted. Reports that can't be sent wait in `files/telemetry-queue.jsonl` (the last 300) and go with the next one that does.

**Playground.** Setup › **Open playground** gives a test field, and under it a card for the last take: the text, what the speech model heard, each cleanup edit and whether the guard kept it, timings, the mic, and the audio stats with a hint when it was quiet, noisy or rumbling. It also gives you a sentence to read out (shown as the field's hint, since on the cover screen the keyboard leaves room for little else). That sentence goes in the report as `expected`, so every playground take is a labelled recording of real phone audio. Keep the same sentence across the **Microphone** chips to compare them.

On the server, `uv run transcriptd telemetry` lists recent reports, one line per take: mic, screen, outcome, latency, speech level, the gap to the noise, rumble, and the word error rate against the sentence read. `--json` prints everything. `--export DIR` writes the read-out takes as `<id>-<mic>.wav` + `.txt`, so `uv run transcriptd bench DIR --profiles english-whisper english` scores any model against them.

## Build

You need JDK 21 (17 works too) and the Android SDK with platform 36 (`platforms;android-36`) and build tools; the Gradle wrapper fetches Gradle 8.14.3 itself, and the Android Gradle plugin installs the build-tools version it wants if your SDK is writable and its licenses are accepted (`sdkmanager --licenses`). Android Studio sets all of this up; without it, install the [command-line tools](https://developer.android.com/studio#command-line-tools-only) and run `sdkmanager "platforms;android-36" "platform-tools"`. Point Gradle at the SDK with `ANDROID_HOME`, or a `local.properties` in `android/` with `sdk.dir=/path/to/Android/Sdk` (it's gitignored).

```sh
export ANDROID_HOME=$HOME/Android/Sdk    # or sdk.dir in android/local.properties
cd android
./gradlew testDebugUnitTest              # plain-Kotlin unit tests, no device needed
./gradlew assembleDebug                  # app/build/outputs/apk/debug/app-debug.apk
```

## Install and pair

1. Install the APK on the phone, either with `adb install -r app-debug.apk` or by copying it over and opening it.
2. Over Tailscale there's nothing to pair. Set `tailnet_auth = true` in the server's `config.toml` and the server lets in your own devices (logged in to Tailscale as the same user as the server) without the token; other people's devices on the tailnet still need it. Build the app with your server baked in, `./gradlew assembleDebug -PtranscriptdServer=http://100.x.y.z:7411` (your server's Tailscale IP), and it connects on first launch. Tailnet login needs the server to see the phone's own Tailscale address, so it works over the plain Tailscale IP. Through `tailscale serve` (`https://your-server.example.ts.net`) every request arrives from the server itself; for tailnet login to work there, point serve at `http://127.0.0.1:7411` and set `trust_proxy_headers = true` (then any local program that can reach port 7411 can claim to be any device), or pair with the token instead. Built without `-PtranscriptdServer`, the app starts unpaired. Otherwise, on the PC, run `uv run transcriptd mobile` in `server/`. Copy the `transcriptd://pair?...` link to the phone, open the app, and tap **Paste pairing link**. The app talks to the server's Tailscale address over plain HTTP; the Tailscale tunnel is already encrypted. Plain `http://` is only allowed to your own network: loopback, private LAN ranges (10/8, 172.16/12, 192.168/16), link-local, Tailscale's 100.64.0.0/10, and their IPv6 counterparts (`ServerUrl.kt`). Any other address has to be `https://`, MagicDNS names (`*.ts.net`) included: with Tailscale off, whatever DNS server the phone is using could answer for that name, and `tailscale serve` gives it HTTPS anyway. The app refuses to save or pair with such an address and says so. It doesn't follow redirects either (a hop wouldn't be checked, and a 307/308 would resend the audio and token): point it at the final address.
3. In the app, allow the **Microphone**, turn on the **Dictation bubble** (Settings › Accessibility › Installed apps › transcriptd dictation bubble), and allow the **Listening notice**. An APK opened from a file (Taildrop, a browser download) gets Android's restricted settings: turning the bubble on says "App was denied access". Allow it once in Settings › Apps › transcriptd › ⋮ › **Allow restricted settings**, then turn it on again. `adb install` doesn't have this restriction.
4. For the cover screen there's nothing extra to set up. The bubble shows up over any app you're typing in there, as long as that app itself is allowed on the cover screen (Settings › Advanced features › Labs › Apps allowed on cover screen, or Good Lock's MultiStar).

## Test on an emulator

Debug builds read `files/test.wav` (16 kHz mono PCM16) instead of the microphone when that file exists, in real time. That way the whole path can run headless.

A `flip7-cover` AVD is a stock Android 16 device with the Flip 7 cover panel: 948×1048 at density 420 (361×399 dp), optionally skinned with Samsung's [Galaxy Z Flip7 emulator skin](https://developer.samsung.com/galaxy-emulator-skin/galaxy-z.html) (cover display). The skin is Samsung's download (it needs a Samsung Account), so it isn't in the repo; unzip it anywhere. The skin is only the frame and camera mask; there's no One UI, so no Samsung Keyboard or Flex Window behaviour. To make it: `sdkmanager "emulator" "system-images;android-36;google_apis;x86_64"`, create any AVD on that image (`avdmanager create avd -n flip7-cover -k "system-images;android-36;google_apis;x86_64"`), then in its `config.ini` (`~/.android/avd/flip7-cover.avd/`) set `hw.lcd.width=948`, `hw.lcd.height=1048`, `hw.lcd.density=420`, and for the skin `skin.name=galaxy-z-flip7-cover` and `skin.path=<the unzipped Galaxy_Z_Flip7(Cover_Display) folder>`. The `emulator` and `adb` commands below are in the SDK's `emulator/` and `platform-tools/`.

```sh
emulator -avd flip7-cover -no-window -no-audio -no-boot-anim -gpu swangle_indirect -port 5582 &
A="adb -s emulator-5582"
$A install -r app/build/outputs/apk/debug/app-debug.apk
$A shell pm grant dev.transcriptd.app android.permission.RECORD_AUDIO
$A shell settings put secure enabled_accessibility_services dev.transcriptd.app/.FlowService
$A shell settings put secure accessibility_enabled 1
$A push speech16k.wav /sdcard/Android/data/dev.transcriptd.app/files/test.wav
$A shell am start -a android.intent.action.VIEW -d "'transcriptd://pair?u=http://10.0.2.2:7411&t=TOKEN'"
$A shell am start -a android.intent.action.SENDTO -d sms:5551234   # Google Messages; tap its field
$A shell input swipe X Y X Y 9000                                   # hold the bubble for 9 s
```

The emulator reaches the PC's `127.0.0.1` at `10.0.2.2`. With `-no-window` the skin isn't drawn, so to see screenshots with the cameras, composite them: the skin's `device_Port-Jetblack.png`, then the screenshot at (193, 220), then `fore_port.png` at the same spot (that's what its `layout` file says). Without the skin AVD, any AVD works after `wm size 948x1048` and `wm density 420`.
