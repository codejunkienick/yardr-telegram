# telegram-voice-transcripts citations

The source file and symbol behind the claims in SKILL.md, one row per claim.
The rows live only here; SKILL.md holds none.

```text
<!-- citations -->
| kind | path | symbol | note |
| pack | bridge/bridge.py | TRANSCRIBE_KINDS = ('voice', 'audio') | both delivery kinds use transcription |
| pack | bridge/bridge.py | note = f'[attachment saved: {path} \| {kind} {duration} \| transcribing]' | first note |
| pack | bridge/bridge.py | return f'[{kind} {duration} transcript (msg {mid}): {body}] (audio: {path})' | follow-up |
| pack | bridge/bridge.py | self._relay(mid, f'{mid}-transcript', text, thread_id=state.get('thread_id'), kind='transcript', | the follow-up is kind transcript and follows its note's topic |
| pack | YARDR.md | The yard keeps at most 500 characters of a delivery's text | a long transcript is cut in the inbox |
| pack | yardr/tgbridge.yaml | read:   [tgbridge/thread, read] | yardr thread show reads beyond the inbox preview, within the bridge cap |
| pack | bridge/bridge.py | TRANSCRIPT_MAX_CHARS = 4000 | bridge transcript text cap |
| pack | bridge/bridge.py | value[:limit - 1] + '\u2026' | a capped transcript ends with an ellipsis |
| pack | transcriber/voice_transcribe.py | _write_text(out, text) | a manual re-run saves the full text without the bridge cap |
| pack | bridge/bridge.py | state['gave_up'] = True | follow-up delivery can stop after relay failures; re-run by hand if none follows within a few minutes |
| pack | bridge/bridge.py | if self.transcribe and failure is None and kind in self.TRANSCRIBE_KINDS: | transcription off leaves a plain saved note |
| pack | bridge/bridge.py | voice transcript queue unavailable (msg {mid}); relaying without transcription | queue write failure also leaves a plain saved note |
| pack | bridge/bridge.py | body = transcript if reason is None else f'transcription failed: {reason}' | failure text |
| pack | bridge/bridge.py | self._relay_to_yard(provider_message_id, f"[topic: {topic['name']}]\n{text}", reply, | topic line first |
| pack | bridge/bridge.py | 'VOICE_TRANSCRIBE_LANGUAGE': language, | configured language reaches the transcriber |
| pack | bridge/bridge.py | is English-only and cannot decode | English-only (.en) model requires language = "en" |
| pack | transcriber/voice_transcribe.py | DEFAULT_LANGUAGE = 'en' | default language |
| pack | transcriber/voice_transcribe.py | self.language = language or env.get('VOICE_TRANSCRIBE_LANGUAGE') or DEFAULT_LANGUAGE | manual language override |
| pack | transcriber/voice_transcribe.py | VOICE_TRANSCRIBE_WHISPER_CLI  whisper-cli binary (~/work/whisper.cpp/build/bin/whisper-cli) | manual whisper-cli path override and default |
| pack | transcriber/voice_transcribe.py | VOICE_TRANSCRIBE_MODEL_DIR    ggml models directory (~/work/whisper.cpp/models) | manual model directory override and default |
| pack | transcriber/voice_transcribe.py | '-l', getattr(options, 'language', DEFAULT_LANGUAGE) | language passed to whisper-cli |
| pack | transcriber/voice_transcribe.py | '--vad', '-vm' | Silero VAD |
| pack | transcriber/voice_transcribe.py | LOOP_MIN_REPEATS = 3 | loop guard |
| pack | transcriber/voice_transcribe.py | argv += ['-mc', '0'] | retry without context |
| pack | install.sh | files[root / 'bin/voice-transcribe'] = ( | the instance's own voice-transcribe launcher runs the installed transcriber without loading bridge transcribe settings |
| pack | install.sh | ".local/share/yardr-telegram" | the default install root |
| pack | install.sh | install.sh --settings <file> [--root <dir>] [--dry-run] | operator runs install.sh with the settings file |
<!-- /citations -->
```
