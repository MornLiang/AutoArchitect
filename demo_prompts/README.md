# Demo prompts for Text2IFC

Each `.txt` file in this folder is a self-contained natural-language prompt
that can be fed to the Text2IFC pipeline via:

```bash
python run_text2ifc.py --prompt-file demo_prompts/<file>.txt
```

| File | Target building | Companion GT IFC |
|---|---|---|
| `1px_two_storey_office.txt` | 2-storey concrete office (15m × 24m, 45 walls / 16 doors / 26 windows / 7 columns / 2 railings) | `../../demo_data/1px(1).ifc` |
| `single_storey_shed.txt` | 1-storey concrete shed (6m × 4m, 1 door / 2 windows / flat roof) | *(none — pure generation)* |

To create a new demo, drop a plain `.txt` file in this folder and reference
it on the CLI.  Comments are NOT stripped — keep the file content purely
descriptive.
