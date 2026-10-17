# telegram-voice-transcripts citations

The source file and symbol behind the claims in SKILL.md, one row per claim.
The rows live only here; SKILL.md holds none.

```text
<!-- citations -->
| kind | path | symbol | note |
| city | packs/telegram/bridge/bridge.py | note = f'[attachment saved: {path} \| {kind} {duration} \| transcribing]' | first note |
| city | packs/telegram/bridge/bridge.py | return f'[{kind} {duration} transcript (msg {mid}): {body}] (audio: {path})' | follow-up |
| city | packs/telegram/bridge/bridge.py | body = transcript if reason is None else f'transcription failed: {reason}' | failure text |
| city | packs/telegram/bridge/bridge.py | 'not_english': 'not English speech', | not English |
| city | packs/telegram/transcriber/voice_transcribe.py | LANGUAGE = 'en' | English forced |
| city | packs/telegram/transcriber/voice_transcribe.py | '--vad', '-vm' | Silero VAD |
| city | packs/telegram/transcriber/voice_transcribe.py | LOOP_MIN_REPEATS = 3 | loop guard |
| city | packs/telegram/transcriber/voice_transcribe.py | argv += ['-mc', '0'] | retry without context |
<!-- /citations -->
```
