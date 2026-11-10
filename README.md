# Voyager's Wake

*A Cinematic Space Odyssey* — an interactive, scroll-driven motion story that
follows a camera plunge from Earth orbit past the Moon, Mars, the asteroid belt,
Jupiter and Saturn to the heliopause. Each chapter plays a short "dive" video,
and "connector" clips bridge one chapter to the next.

The site is plain static HTML/CSS/JS with no build step. The video clips were
generated with Google Veo (Vertex AI) by the Python pipeline in `scripts/`.

## Run locally

Serve the repository root over HTTP (opening `index.html` directly via
`file://` will not load `content/story.json`):

```sh
python3 scripts/serve_project.py .            # http://127.0.0.1:8080
python3 scripts/serve_project.py . --port 3000 --host 0.0.0.0
```

`serve_project.py` is a small static server that refuses hidden files such as
`.env`. Any other static file server works too.

## Repository layout

| Path | Contents |
| --- | --- |
| `index.html`, `styles.css` | Page shell and styling |
| `src/app.js`, `src/motion-runtime.js` | Story rendering and scroll/video playback runtime |
| `content/story.json` | Story text, chapters and playback settings (`meta`, `chapters`) |
| `assets/video/dives/` | One clip per chapter |
| `assets/video/connectors/` | Transition clips between chapters |
| `assets/video/frames/` | First/last frames extracted from every clip |
| `assets/images/` | Scene/character image masters (currently empty) |
| `prompts/video-manifest.json` | Veo settings and the list of clips (id, prompt file, output path) |
| `prompts/style-motion.txt`, `prompts/style-preamble.txt` | Shared style text |
| `prompts/video/{dives,connectors}/` | Per-clip prompts |
| `scripts/` | Video pipeline and media utilities |
| `docs/` | Placeholder for documentation |

## Video generation pipeline

`scripts/veo_pipeline.py` reads `prompts/video-manifest.json` and, for each
dive and connector:

1. Prepends `prompts/style-motion.txt` to the clip's prompt file.
2. Submits a long-running Veo request to the Vertex AI endpoint
   (model, aspect ratio, duration, etc. come from the manifest), polls until it
   finishes and downloads the raw video to `tmp/veo/raw/` (request/operation/result
   records go to `tmp/veo/tasks/`).
3. Normalizes it with ffmpeg to the manifest's target size, fps and duration,
   writing the manifest's `output` path.
4. Extracts first and last frames into `assets/video/frames/`.

Existing non-empty outputs are skipped unless `--force` is passed.

### Prerequisites

- Python 3 with `google-auth` (Vertex AI auth) and `Pillow` (for the
  contact-sheet and poster utilities)
- `ffmpeg` and `ffprobe` on `PATH`
- A Google Cloud project with the Vertex AI API enabled

### Configuration

Set these in the environment or in a `.env` file at the repo root (`.env` is
gitignored; never commit credentials):

| Variable | Purpose |
| --- | --- |
| `GCP_PROJECT_ID` (or `VERTEX_PROJECT_ID`) | Required Google Cloud project |
| `GCP_LOCATION` (or `VERTEX_LOCATION`) | Region, defaults to `us-central1` |
| `GCP_SERVICE_ACCOUNT_KEY_FILE` | Optional path to a service-account JSON key (Vertex AI User role) |

Without a key file the client falls back to Application Default Credentials
(`gcloud auth application-default login`).

### Commands

Run from the repository root (the pipeline imports `scripts.veo_client`, so use
module mode):

```sh
python3 -m scripts.veo_pipeline .              # plan: print the clip plan, no API calls
python3 -m scripts.veo_pipeline . dives        # generate chapter clips
python3 -m scripts.veo_pipeline . connectors   # generate transition clips
python3 -m scripts.veo_pipeline . all --workers 4 --attempts 2
```

Other options: `--limit N`, `--force`, `--poll-seconds`, `--timeout-seconds`.

Utilities:

```sh
python3 scripts/verify_media.py .              # check codec/size/fps/duration of every clip
python3 scripts/contact_sheet.py .             # midpoint contact sheet -> tmp/contact-sheet.png
python3 scripts/make_posters.py .              # assets/images/scenes/*.png -> WebP posters
```

## Deployment

The site is deployed on Vercel as a static site. No build command is needed:
serve the repository root as-is (`index.html` is the entry point).
