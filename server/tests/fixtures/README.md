# Fixtures

`jfk.wav` is the public-domain sample from transcribe.cpp, used by the tests.

## Bake-off set (record these yourself)

The model choice for the trilingual profile is decided by numbers on your own voice, not
by model cards. Record each line below as its own 16 kHz mono WAV (`<name>.wav`) with the
exact reference text in `<name>.txt` (write it in the script you mean, Devanagari for
Hindi/Marathi words, Latin for English words). Then:

```
uv run transcriptd bench tests/fixtures/bakeoff --profiles english multilingual
```

Any recorder works; convert with `ffmpeg -i in.m4a -ar 16000 -ac 1 out.wav`.

### English negation set (`neg-en-*.wav`)

1. never merge a change like this
2. do not deploy before Friday
3. I did not sign the contract
4. we should never ship without tests
5. this is not ready, do not release it yet
6. I haven't reviewed it, so don't merge
7. nobody approved this
8. only after the backup completes, not before

### Hindi negation set (`neg-hi-*.wav`)

1. यह अभी नहीं चलेगा
2. इसे शुक्रवार से पहले मत भेजना
3. मैंने कभी नहीं कहा कि यह ठीक है
4. बिना टेस्ट के कुछ भी deploy नहीं करना

### Marathi negation set (`neg-mr-*.wav`)

1. हे कधीच merge करू नका
2. आम्ही शुक्रवारच्या आधी जाणार नाही
3. मला हे नको आहे
4. टेस्ट झाल्याशिवाय release करायचं नाही

### Code-switching set (`cs-*.wav`)

1. Hello everyone आम्ही इथे x प्रोजेक्टबद्दल बोलण्यासाठी आलो आहोत
2. कल की meeting में हमने decide किया कि deadline Friday है
3. the API is broken पण मी आज fix करू शकत नाही
4. मुझे लगता है we should not merge this before the review
5. Kubernetes cluster मध्ये तीन nodes down आहेत

### Whispered set (`whisper-*.wav`)

Re-record three or four of the negation sentences whispering, at the distance you would
actually dictate from. Whispered speech is unvoiced, which is a different problem from
being quiet; the server boosts quiet input automatically, but only recordings show
whether the models keep "not" and "never" when whispered.

### Vocabulary set (`vocab-*.wav`)

Say a few names and terms the models will not know: your name, your colleagues' names,
product names, project codenames. These drive the phonetic matcher tests.

## What the bench reports

- `WER%`: word error rate against the reference (script-aware normalizer).
- `prot`: protected words (negations/temporal words) in the reference that survived.
  This is the number that matters most.
- `script`: rough fraction of words emitted in the same script as the reference at the
  same position (catches wholesale transliteration).
