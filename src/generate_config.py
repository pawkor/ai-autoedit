#!/usr/bin/env python3
"""
generate_config.py — generate per-event config.ini from a natural language description.

Uses Claude API (claude-haiku) to produce CLIP prompts and scene_selection settings
tailored to the described footage. Writes config.ini in the current directory.

Usage:
    python3 /path/to/repo/generate_config.py "description of the day's ride"
    python3 /path/to/repo/generate_config.py "description" --preview   # print only, don't write
"""

import argparse
import os
import sys
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic not installed. Run: pip install anthropic", file=sys.stderr)
    sys.exit(1)

SYSTEM_PROMPT = """You are a config generator for an AI motorcycle video highlight pipeline.

The pipeline uses OpenCLIP ViT-L-14 to score video scenes against text prompts.
Final score = pos_score - neg_score * neg_weight (typical range 0.13–0.22).

Your job: given a description of a day's motorcycle ride, generate a config.ini
with [clip_prompts] and [scene_selection] sections optimised for that footage.

Rules for prompts:
- Write in English regardless of input language
- Positive prompts: describe exactly what IS visible in the footage — specific terrain,
  road type, lighting, landmarks. The more specific, the better CLIP discriminates.
  Do NOT add locations or conditions not mentioned in the description.
- IMPORTANT: CLIP scores individual still frames, NOT motion or action. Never write
  prompts about camera movement, speed, riding action, or anything that requires
  seeing multiple frames (e.g. "motorcycle leaning", "sweeping curves", "ascending",
  "slow crawling"). Describe only static visual content visible in a single frame.
- Use proper nouns from the description: hill names, lake names, town names, road names.
  CLIP is trained on internet images and recognises named landmarks well.
  E.g. "Wieżyca hill Kashubia" scores higher than "hill with tower".
- If the description mentions a passenger or group, include back-camera perspective prompts:
  "two helmets visible on motorcycle from behind", "rider with passenger rear view",
  "motorcycle with pillion passenger", "two riders on motorcycle back view".
- If the description mentions a group ride, include: "group of motorcycles on road",
  "motorcycle convoy on rural road", "multiple motorcycles in formation".
- Negative prompts: describe what should be excluded — bad scenes, blurry footage,
  dangerous/unwanted moments mentioned in the description. Do NOT negate motion/action.
- 10–16 positive prompts, 5–8 negative prompts.
- One prompt per line, each line is a short descriptive phrase (no punctuation at end).

Rules for scene_selection:
- threshold: 0.130–0.155. Lower for pastoral/dark/unusual footage, higher for dramatic
  mountain passes. Default 0.148.
- max_per_file_sec: 45–90. Higher if fewer source files expected.
- max_scene_sec: 10–12.
- min_take_sec: 3 (always).
- neg_weight: 0.3–0.5. Lower (0.3) if footage is dark, shadowy, or unusual lighting.
  Default 0.5.
- Add tier overrides only if clearly needed.

Output ONLY valid INI content, no markdown, no explanation, no code blocks.
Start directly with [clip_prompts].

Example output format:
[clip_prompts]
positive =
    motorcycle riding winding road through dense forest
    narrow forest road with dappled sunlight
    ...

negative =
    boring flat highway with no scenery
    ...

[clip_scoring]
neg_weight = 0.4

[scene_selection]
threshold = 0.138
max_per_file_sec = 60
max_scene_sec = 12
min_take_sec = 3
"""


def generate(description: str) -> str:
    client = anthropic.Anthropic()

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Generate config.ini for this day's footage:\n\n{description}",
            }
        ],
    )

    return message.content[0].text.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("description", help="Natural language description of the day's ride")
    parser.add_argument("--preview", action="store_true", help="Print config without writing file")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    print("  Generating config from description...", flush=True)
    config_text = generate(args.description)

    if args.preview:
        print(config_text)
        return

    output_path = Path.cwd() / "config.ini"

    if output_path.exists():
        backup = output_path.with_suffix(".ini.bak")
        output_path.rename(backup)
        print(f"  Existing config.ini backed up to config.ini.bak")

    output_path.write_text(config_text + "\n")
    print(f"  Written: {output_path}")
    print()
    print(config_text)


if __name__ == "__main__":
    main()
