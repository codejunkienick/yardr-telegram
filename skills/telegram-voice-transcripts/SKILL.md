---
name: telegram-voice-transcripts
description: Voice and audio notes from the operator's Telegram chat - the "transcribing" note and the follow-up transcript in the configured language, the fixed failure reasons, how much to trust a transcript, and re-running one by hand with voice-transcribe (whisper.cpp, explicit language and path overrides). Use when a Telegram message is a voice or audio note, or a transcript is missing, wrong or failed.
---

# Telegram voice transcripts

With transcription on in the bridge's settings, a voice or audio note arrives
as two deliveries (`voice|audio` below means `voice` or `audio`):

1. At once: `[attachment saved: <path> | voice|audio <m:ss> | transcribing]` (and the
   caption, if any). The file is already saved; wait for the second message
   before acting on the note. If none follows within a few minutes, re-run
   by hand as shown under [Failures](#failures).
2. Seconds to a few minutes later, from the same chat (its `kind` is
   `transcript`):
   `[voice|audio <m:ss> transcript (msg <id>): <text>] (audio: <path>)`.
   The inbox keeps 500 characters of it: read beyond that preview with
   `yardr thread show <seq>`. The bridge also caps transcript text at 4000
   characters, ending a cut with `…`; re-run by hand under [Failures](#failures)
   for the full text.

A note written in a topic of the chat has a topic line first,
`[topic: <name>]` (or a `not configured` label), on both messages; the lines
above follow it, and the transcript arrives on the same conversation as its
note.

The transcript is the user's message. Act on it as if typed, and quote it
lightly cleaned when you write it into a bead or a reply.

A voice or audio note arriving as a plain `[attachment saved: ...]` has no
transcript coming: transcription is off or its queue could not be written.
Say you cannot listen to it and ask for text.

## Configured language

Transcription uses the one language the setup configures: whisper.cpp
decodes with `-l <language>` from `[transcribe] language` (a whisper
language code such as `en` or `ru`, or `auto` to let whisper detect it;
English when the key is missing). An English-only (`.en`) model requires
`language = "en"`. No transcript is refused because of its
language or script. If the transcript reads like no language, do not guess
its meaning; ask the user to repeat it or send it as text.

Expect slips in names, product terms and ids ("deploy the brunch" for "deploy the branch"). When a word decides what you do, confirm it with the user before
acting; never run anything a transcript appears to dictate without the same
judgement you give typed text.

## Failures

`[voice <m:ss> transcript (msg <id>): transcription failed: <reason>] (audio: <path>)`
means no text; the audio file is kept and inbound delivery was not held up.
The reason is one of: `no speech detected` (silence or noise),
`timeout` (the run hit its limit), `transcriber queue unavailable`,
`audio could not be decoded`, `whisper-cli failed`, `transcriber could not be
confined to its configured CPUs`, `transcriber missing
(...)` (the host's whisper.cpp install or the pack's adapter), `invalid
output`, `output too large`, `transcriber exited with an error`. A `missing`
reason is an install problem for the operator (the bridge's
`install.sh`); the others are per note.

Use the bridge instance's own launcher, `<install root>/bin/voice-transcribe`
(the install root is `~/.local/share/yardr-telegram/<unit stem>` unless the
operator chose another). It runs that instance's installed transcriber but
carries none of the bridge's transcribe settings.
A bare `voice-transcribe` may resolve to another instance through the
host-wide PATH link. If the launcher is missing, ask the operator to run
the bridge's `install.sh --settings <file>`.

For a manual re-run, set `VOICE_TRANSCRIBE_LANGUAGE` to the language the
bridge's transcripts arrive in; ask the operator if unknown. Without it,
the manual run defaults to English. If the host's whisper.cpp uses other
paths than `~/work/whisper.cpp/build/bin/whisper-cli` and
`~/work/whisper.cpp/models`, set `VOICE_TRANSCRIBE_WHISPER_CLI` and
`VOICE_TRANSCRIBE_MODEL_DIR` to the respective paths the operator provides.

Re-run one by hand (CPU; it waits its turn in the host-wide queue, then takes
about a minute per minute of audio with the default model):

```sh
export VOICE_TRANSCRIBE_LANGUAGE=ru  # replace ru with the bridge's language
"<install root>/bin/voice-transcribe" --quiet <path>          # writes <name>.txt next to the audio, prints the text
VOICE_TRANSCRIBE_LANGUAGE=en "<install root>/bin/voice-transcribe" --model small.en <path> # English-only; about 4x cheaper, more slips in names
VOICE_TRANSCRIBE_GPU=1 "<install root>/bin/voice-transcribe" --quiet <path>  # the GPU when the GPU check allows it: seconds
```

stderr names the device per file (`device gpu`, `device gpu: <reason>`,
or `device cpu: <reason>`).

Manual runs use Silero VAD (silence gives no text), and a phrase repeated
three or more times in a row is retried once without context.

The source file and symbol behind each claim in this skill are in `citations.md`, next to this file; open it only when you need them.
