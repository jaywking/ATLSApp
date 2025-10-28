locations-app/
├─ app/
│  ├─ main.py                 # NiceGUI + FastAPI entrypoint
│  ├─ ui/                     # NiceGUI pages
│  │  ├─ __init__.py
│  │  ├─ layout.py            # sidebar/nav, toasts, theming
│  │  ├─ home.py              # dashboard tiles, recent jobs
│  │  ├─ locations.py         # table, actions, open in Notion/Maps
│  │  ├─ maps.py              # Google Maps embed page/panel
│  │  ├─ files.py             # file browser, previews (PDF/images)
│  │  └─ jobs.py              # job list, status, logs
│  ├─ api/                    # FastAPI routers
│  │  ├─ __init__.py
│  │  ├─ locations.py         # process/reprocess endpoints
│  │  ├─ files.py             # upload/download/preview endpoints
│  │  └─ jobs.py              # enqueue, status, logs
│  ├─ services/               # pure-Python helpers
│  │  ├─ __init__.py
│  │  ├─ notion_service.py
│  │  ├─ googlemaps_service.py
│  │  ├─ lha_service.py       # docx→pdf render, output paths
│  │  ├─ storage_service.py   # S3/R2 signed URLs, thumbnails
│  │  └─ jobs_service.py      # enqueue, fetch status
│  ├─ workers/                # background jobs
│  │  └─ worker.py            # RQ/Celery/Arq (pick one)
│  ├─ static/                 # pdf.js, icons, custom CSS
│  ├─ templates/              # html snippets (if needed)
│  └─ config.py               # settings from .env
├─ tests/
│  └─ test_smoke.py
├─ .env.sample
├─ requirements.txt
├─ requirements-dev.txt
├─ README.md
├─ docker-compose.yml         # api, worker, redis, nginx
└─ Makefile                   # or scripts/ for common tasks
