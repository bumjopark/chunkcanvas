"""Fetch the pix2text-mfr 1.5 formula recognizer (MIT, published as ONNX) and save it as a
PyTorch VisionEncoderDecoder checkpoint, so decoding can cache attention (several times faster
on CPU than the published decoder, which recomputes the image attention for every token).

    services/api/.venv/bin/python scripts/convert_pix2text.py services/api/.models/pix2text-mfr-1.5

Reading the ONNX weights needs the `onnx` package; when the venv lacks it, a throwaway venv is
made for that step so the app's own dependencies stay as locked.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "breezedeus/pix2text-mfr-1.5"
REVISION = "1cef9f0bdcd6a4c63df7de1311fb0894593340cc"
# transformers 5 names DeiT encoder layers differently from the ONNX export.
RENAME = [
    (r"^encoder\.encoder\.layer\.(\d+)\.attention\.attention\.query\.", r"encoder.layers.\1.attention.q_proj."),
    (r"^encoder\.encoder\.layer\.(\d+)\.attention\.attention\.key\.", r"encoder.layers.\1.attention.k_proj."),
    (r"^encoder\.encoder\.layer\.(\d+)\.attention\.attention\.value\.", r"encoder.layers.\1.attention.v_proj."),
    (r"^encoder\.encoder\.layer\.(\d+)\.attention\.output\.dense\.", r"encoder.layers.\1.attention.o_proj."),
    (r"^encoder\.encoder\.layer\.(\d+)\.intermediate\.dense\.", r"encoder.layers.\1.mlp.fc1."),
    (r"^encoder\.encoder\.layer\.(\d+)\.output\.dense\.", r"encoder.layers.\1.mlp.fc2."),
    (r"^encoder\.encoder\.layer\.(\d+)\.", r"encoder.layers.\1."),
]

EXTRACT = r'''
import sys, numpy as np, onnx
from onnx import numpy_helper
src, out = sys.argv[1], sys.argv[2]
weights = {}
for name, prefix in (("encoder_model.onnx", "encoder."), ("decoder_model.onnx", "")):
    m = onnx.load(f"{src}/{name}")
    inits = {i.name: numpy_helper.to_array(i) for i in m.graph.initializer}
    weights.update({prefix + k: v for k, v in inits.items() if not k.startswith("onnx::")})
    # Linear weights were folded into anonymous MatMul inputs (transposed); the node name gives the module.
    for node in m.graph.node:
        for x in node.input:
            if x.startswith("onnx::") and x in inits:
                path = node.name.strip("/").rsplit("/", 1)[0].replace("/", ".")
                key = "encoder." + path if prefix else path.replace("decoder.decoder.", "decoder.model.decoder.", 1)
                weights[key + ".weight"] = inits[x].T.copy()
np.savez(out, **weights)
'''


def main() -> None:
    dest = Path(sys.argv[1])
    from huggingface_hub import snapshot_download

    src = Path(snapshot_download(REPO, revision=REVISION))
    with tempfile.TemporaryDirectory() as tmp:
        npz = Path(tmp) / "weights.npz"
        script = Path(tmp) / "extract.py"
        script.write_text(EXTRACT)
        try:
            import onnx  # noqa: F401

            python = sys.executable
        except ImportError:
            subprocess.run([sys.executable, "-m", "venv", f"{tmp}/venv"], check=True)
            python = f"{tmp}/venv/bin/python"
            subprocess.run([python, "-m", "pip", "install", "--quiet", "onnx", "numpy"], check=True)
        subprocess.run([python, "-I", str(script), str(src), str(npz)], check=True)

        import numpy as np
        import torch
        from transformers import VisionEncoderDecoderConfig, VisionEncoderDecoderModel

        model = VisionEncoderDecoderModel(VisionEncoderDecoderConfig.from_pretrained(src)).eval()
        state = {}
        for key, value in np.load(npz).items():
            for pattern, repl in RENAME:
                key, n = re.subn(pattern, repl, key)
                if n:
                    break
            state[key] = torch.from_numpy(value)
        missing, unexpected = model.load_state_dict(state, strict=False)
        if unexpected or [m for m in missing if not m.startswith("encoder.pooler.")]:
            raise SystemExit(f"weights did not match: missing {missing[:5]}, unexpected {unexpected[:5]}")
    dest.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(dest)
    for name in ("tokenizer.json", "README.md"):
        shutil.copy(src / name, dest / name)
    (dest / "SOURCE.json").write_text(json.dumps({"repo": REPO, "revision": REVISION, "license": "MIT"}))
    print(f"pix2text-mfr ready in {dest}")


if __name__ == "__main__":
    main()
