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

The pipeline uses OpenCLIP ViT-L-14 to score video frames against text prompts.
Final score = pos_score - neg_score * neg_weight (typical range 0.13–0.22).

Your job: given a description of a day's motorcycle ride, generate a config.ini
with [clip_prompts] and [scene_selection] sections optimised for that footage.

═══ CRITICAL RULE — CLIP sees still frames, never motion ═══
CLIP scores individual still frames, NOT video. Never write prompts about
camera movement, speed, riding dynamics, or anything requiring multiple frames:
❌ "motorcycle leaning into curve", "sweeping descent", "fast cornering", "ascending road"
✅ "narrow road carved into cliff face", "hairpin bend visible from above", "fjord below road"
Every prompt must describe something a person could see in a single photograph.

═══ Location knowledge — USE IT ═══
If the description mentions named locations, roads, or landmarks, draw on your
geographic knowledge to add specific visual details you are confident about.
Examples:
• "Lysebotn" → 27-hairpin descent to Lysefjord, narrow road on cliff face above fjord
• "Preikestolen" → sheer plateau cliff 604m above Lysefjord
• "Trollstigen" → waterfall visible beside road, steep valley walls
• "Stelvio Pass" → high-altitude hairpins with Alpine panorama, elevation markers
• "Transalpina" → exposed ridge road, Romanian Carpathians, open treeless plateau
Only include geographic details you can verify from your training data.
If unsure about a place, describe only what the user explicitly mentioned.

═══ Prompt quality rules ═══
- Write in English regardless of input language
- Positive: specific visual content in single frames — terrain, road surface,
  landscape features, named landmarks, sky/weather only if explicitly mentioned
- Proper nouns score better than generic: "Lysefjord visible below" > "fjord view"
- Do NOT add invented atmosphere or generic aesthetic adjectives:
  ❌ "dramatic scenery", "stunning landscape", "beautiful view" (CLIP ignores these)
  ✅ "rocky mountainside with road switchbacks", "deep fjord between steep cliff walls"
- If passenger/group mentioned: add rear-view helmet prompts
- If group ride mentioned: add convoy/formation prompts
- Negative: bad scene types, obstructions, conditions to exclude
- 10–16 positive prompts, 5–8 negative prompts
- One short phrase per line, no punctuation at end

═══ scene_selection rules ═══
- threshold: 0.130–0.155. Lower for dark/overcast/tunnel-heavy footage, higher for dramatic open landscapes
- max_per_file_sec: 45–90. Higher for fewer source files
- max_scene_sec: 10–12
- min_take_sec: 3 (always)
- neg_weight: 0.3–0.5. Lower for dark or shadowy footage. Default 0.5

Output ONLY valid INI content. No markdown, no explanation, no code blocks.
Start directly with [clip_prompts].

Example output format:
[clip_prompts]
positive =
    narrow road carved into cliff face above fjord
    hairpin bend with steep drop to valley below
    ...

negative =
    parking lot or gas station stop
    blurry frame or lens obstruction
    ...

[clip_scoring]
neg_weight = 0.4

[scene_selection]
threshold = 0.138
max_per_file_sec = 60
max_scene_sec = 12
min_take_sec = 3
"""


def generate(description: str, global_context: str = "") -> str:
    client = anthropic.Anthropic()

    if global_context.strip():
        user_content = (
            f"Global filming context (applies to all projects):\n{global_context.strip()}"
            f"\n\n---\n\nToday's footage description:\n{description}"
        )
    else:
        user_content = f"Generate config.ini for this day's footage:\n\n{description}"

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    return message.content[0].text.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("description", help="Natural language description of the day's ride")
    parser.add_argument("--preview", action="store_true", help="Print config without writing file")
    parser.add_argument("--global-context", default="", help="Global filming context prepended to description")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    print("  Generating config from description...", flush=True)
    config_text = generate(args.description, args.global_context)

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
