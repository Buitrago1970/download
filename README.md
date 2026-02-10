# Downtify MVP

App web para descargar audio desde Spotify, YouTube o texto libre. La UI actual es minimalista y oscura, con flujo directo de vista previa y descarga.

## Funcionalidades
- Input unico: URL de Spotify, URL de YouTube o texto (titulo/cancion).
- Vista previa de metadata con `POST /api/preview`.
- Descarga individual con `POST /api/download`.
- Soporte de playlists de Spotify en segundo plano con `POST /api/playlist/start`, `GET /api/playlist/status/{job_id}`, `GET /api/playlist/file/{job_id}/{file_id}` y `GET /api/playlist/download/{job_id}`.
- Metadata embebida (titulo, artista, album, cover y tags extra cuando existen).

## Formato de salida actual
Actualmente el backend esta configurado para descargar en `mp3` por defecto para canciones y tracks de playlist.

## Credenciales Spotify (opcionales)
Si defines credenciales, la resolucion de playlists usa Spotify Web API y mejora cobertura:

```bash
SPOTIFY_CLIENT_ID=tu_client_id
SPOTIFY_CLIENT_SECRET=tu_client_secret
```

Sin credenciales, se usa fallback publico.

## Rendimiento (playlists)
La descarga de playlists ahora corre en paralelo. Puedes ajustar el numero de workers con:

```bash
PLAYLIST_WORKERS=3
```

Rango efectivo: `1` a `8` (por defecto `3`).

## Desarrollo local

Backend:

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Con ambos procesos arriba, abre `http://localhost:5173`.

## Docker

```bash
docker compose up --build
```

Abre `http://localhost:8000`.

## YouTube cookies (opcional)
Si YouTube bloquea descargas, puedes pasar un archivo de cookies:

```bash
docker run --rm -p 8000:8000 \
  -e YTDLP_COOKIES=/cookies/youtube.txt \
  -v /ruta/a/youtube.txt:/cookies/youtube.txt:ro \
  alacrandw-app:latest
```

Tambien puedes pasar cookies en base64 con `YTDLP_COOKIES_B64` (contenido del archivo Netscape cookie file codificado en base64).
