# Downtify MVP

MVP simple: pega un link de Spotify, YouTube o un titulo y descarga un MP3 con metadata y caratula embebida.

## Que hace
- Acepta input unificado: link publico de Spotify, link de YouTube o texto libre (titulo/cancion).
- Resuelve metadata usando fuentes publicas (Spotify OpenGraph/oEmbed/JSON-LD y YouTube via yt-dlp).
- Si llega Spotify o texto libre, busca la mejor coincidencia en YouTube.
- Si llega YouTube, descarga directo ese video.
- Descarga audio en MP3 (mejor calidad disponible) y embebe caratula + metadata ID3.
- Si llega playlist de Spotify, crea un job en background y entrega un ZIP con multiples MP3.

## Metadata embebida (sin credenciales de Spotify)
- Basica: titulo, artista, album, caratula.
- Extendida cuando existe: duracion, fecha de lanzamiento/publicacion, canal/uploader, source id/url (Spotify ID o YouTube ID).

## Limitaciones actuales
- Sin OAuth de Spotify, solo se usa metadata publica de sus paginas.
- Para playlists de Spotify sin credenciales, usa fallback publico y puede no traer todos los tracks.
- La coincidencia de YouTube para texto o Spotify es automatica y puede variar.

## Credenciales Spotify (opcionales, recomendado para playlists)
Si defines credenciales, las playlists se resuelven con Spotify Web API y mejor cobertura:

```bash
SPOTIFY_CLIENT_ID=tu_client_id
SPOTIFY_CLIENT_SECRET=tu_client_secret
```

Sin credenciales, el backend intenta resolver la playlist con extractor publico.

## Endpoints de playlist
- `POST /api/playlist/start` con `{ \"input\": \"https://open.spotify.com/playlist/...\" }`
- `GET /api/playlist/status/{job_id}`
- `GET /api/playlist/file/{job_id}/{file_id}` para descargar una cancion ya lista
- `GET /api/playlist/download/{job_id}`

`/api/playlist/status/{job_id}` devuelve `files` con las canciones disponibles hasta el momento para descarga incremental.

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

## Docker (recomendado)
```bash
docker compose up --build
```
Abre `http://localhost:8000`.

## YouTube cookies (opcional, recomendado si falla la descarga)
YouTube puede bloquear descargas sin cookies. Si te pasa, exporta cookies de tu navegador y pásalas al contenedor:

```bash
docker run --rm -p 8000:8000 \
  -e YTDLP_COOKIES=/cookies/youtube.txt \
  -v /ruta/a/youtube.txt:/cookies/youtube.txt:ro \
  alacrandw-app:latest
```

También puedes usar `docker compose` agregando `environment` y `volumes` en `docker-compose.yml`.
