# telegram-voice-transcripts citations

The source file and symbol behind the claims in SKILL.md, one row per claim.
The rows live only here; SKILL.md holds none.

```text
<!-- citations -->
| kind | path | symbol | note |
| pack | bridge/bridge.py | note = f'[attachment saved: {path} \| {kind} {duration} \| transcribing]' | first note |
| pack | bridge/bridge.py | return f'[{kind} {duration} transcript (msg {mid}): {body}] (audio: {path})' | follow-up |
| pack | bridge/bridge.py | self._relay(mid, f'{mid}-transcript', text, thread_id=state.get('thread_id'), kind='transcript', | the follow-up is kind transcript and follows its note's topic |
| pack | YARDR.md | The yard keeps at most 500 characters of a delivery's text | a long transcript is cut in the inbox |
| pack | yardr/tgbridge.yaml | read:   [tgbridge/thread, read] | yardr thread show reads it whole |
| pack | bridge/bridge.py | body = transcript if reason is None else f'transcription failed: {reason}' | failure text |
| pack | bridge/bridge.py | self._relay_to_yard(provider_message_id, f"[topic: {topic['name']}]\n{text}", reply, | topic line first |
| pack | bridge/bridge.py | 'VOICE_TRANSCRIBE_LANGUAGE': language, | configured language reaches the transcriber |
| pack | bridge/bridge.py | is English-only and cannot decode | English-only (.en) model requires language = "en" |
| pack | transcriber/voice_transcribe.py | DEFAULT_LANGUAGE = 'en' | default language |
| pack | transcriber/voice_transcribe.py | '-l', getattr(options, 'language', DEFAULT_LANGUAGE) | language passed to whisper-cli |
| pack | transcriber/voice_transcribe.py | '--vad', '-vm' | Silero VAD |
| pack | transcriber/voice_transcribe.py | LOOP_MIN_REPEATS = 3 | loop guard |
| pack | transcriber/voice_transcribe.py | argv += ['-mc', '0'] | retry without context |
| pack | install.sh | files[root / 'bin/voice-transcribe'] = ( | the instance's own voice-transcribe launcher |
| pack | install.sh | ".local/share/yardr-telegram" | the default install root |
| pack | install.sh | install.sh --settings <file> [--root <dir>] [--dry-run] | operator runs install.sh with the settings file |
<!-- /citations -->
```
