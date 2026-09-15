"""Local BiRefNet alpha-only cutouts using the installed 2K16 model pipeline."""
from functools import lru_cache
import hashlib
import json
from PIL import Image, ImageChops


@lru_cache(maxsize=1)
def session_for(directory):
    import onnxruntime as ort
    metadata = json.loads((directory / 'model.json').read_text())
    model = directory / metadata['filename']
    if hashlib.sha256(model.read_bytes()).hexdigest() != metadata['sha256']:
        raise ValueError('Background removal model failed its integrity check.')
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    return ort.InferenceSession(str(model), sess_options=options, providers=['CPUExecutionProvider']), metadata


def remove_background(image, directory, progress=lambda text: None, cancelled=lambda: False):
    import numpy as np
    def checkpoint():
        if cancelled(): raise InterruptedError('Background removal cancelled.')
    checkpoint(); progress('Loading local background-removal model…')
    session, meta = session_for(directory)
    checkpoint(); progress('Separating the player locally. This can take a minute…')
    rgb = image.convert('RGB').resize(tuple(meta['expected_input_size']), Image.Resampling.LANCZOS)
    values = np.asarray(rgb, dtype=np.float32) / 255.0
    values = (values - np.array(meta['normalization']['mean'], dtype=np.float32)) / np.array(meta['normalization']['std'], dtype=np.float32)
    tensor = np.transpose(values, (2, 0, 1))[None, ...].astype(np.float32)
    logits = np.asarray(session.run(None, {session.get_inputs()[0].name: tensor})[0], dtype=np.float32)
    checkpoint()
    while logits.ndim > 2: logits = logits[0]
    prediction = 1 / (1 + np.exp(-np.clip(logits, -80, 80)))
    low, high = float(prediction.min()), float(prediction.max())
    if high <= low: raise ValueError('The model returned an empty mask.')
    mask = Image.fromarray(np.rint((prediction - low) / (high - low) * 255).astype(np.uint8))
    mask = mask.resize(image.size, Image.Resampling.LANCZOS)
    result = image.copy()
    # Never replace RGB pixels or restore transparency from the source image.
    result.putalpha(ImageChops.multiply(image.getchannel('A'), mask))
    checkpoint(); return result
