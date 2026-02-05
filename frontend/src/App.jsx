import React, { useEffect, useState } from "react";

const API_BASE = "";

async function postJson(path, payload) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || "Error de servidor");
  }
  return res.json();
}

export default function App() {
  const [input, setInput] = useState("");
  const [format] = useState("mp3");
  const [meta, setMeta] = useState(null);
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [includeLrc, setIncludeLrc] = useState(false);
  const [playlistJobId, setPlaylistJobId] = useState("");
  const [playlistJob, setPlaylistJob] = useState(null);
  const [lastPayload, setLastPayload] = useState(null);
  const [canDownload, setCanDownload] = useState(false);

  useEffect(() => {
    if (!playlistJobId) return undefined;
    const timer = setInterval(async () => {
      try {
        const res = await fetch(`/api/playlist/status/${playlistJobId}`);
        if (!res.ok) return;
        const job = await res.json();
        setPlaylistJob(job);
        setInfo(`Playlist: ${job.done || 0}/${job.total || 0} completadas (${job.failed || 0} fallidas)`);
        if (job.status === "failed") {
          setError(job.error || "Fallo la descarga de la playlist.");
          setStatus("ready");
          clearInterval(timer);
          return;
        }
        if (job.status === "done") {
          setStatus("done");
          clearInterval(timer);
        }
      } catch (_) {
        // ignore temporary polling errors
      }
    }, 2000);
    return () => clearInterval(timer);
  }, [playlistJobId]);

  useEffect(() => {
    if (!input || !input.trim()) {
      setMeta(null);
      setCanDownload(false);
      setError("");
      return undefined;
    }

    setError("");
    setInfo("");
    setCanDownload(false);
    const timer = setTimeout(async () => {
      try {
        const data = await postJson("/api/preview", { input });
        setMeta(data);
        setCanDownload(true);
      } catch (err) {
        setMeta(null);
        setCanDownload(false);
        setError("No pude encontrar esa entrada.");
      }
    }, 450);

    return () => clearTimeout(timer);
  }, [input]);

  const handleDownload = async () => {
    setError("");
    setInfo("");
    setPlaylistJobId("");
    setPlaylistJob(null);
    setStatus("downloading");
    setLastPayload({ input, format, includeLrc });
    try {
      const preview = meta || (await postJson("/api/preview", { input }));
      if (preview) setMeta(preview);
      if (isSpotifyPlaylistInput(input, preview)) {
        const started = await postJson("/api/playlist/start", { input, format, include_lrc: includeLrc });
        const jobId = started.job_id;
        if (!jobId) throw new Error("No se pudo iniciar el job de playlist");
        setPlaylistJobId(jobId);
        setInfo("Playlist en proceso. Puedes descargar canciones en cuanto aparezcan abajo.");
        return;
      }

      const res = await fetch(`/api/download`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input, format, include_lrc: includeLrc })
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "Error en descarga");
      }
      const blob = await res.blob();
      const filename = getFileName(res.headers.get("Content-Disposition")) || "download.mp3";
      const link = document.createElement("a");
      link.href = window.URL.createObjectURL(blob);
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setStatus("done");
    } catch (err) {
      setError(err?.message || "No pude procesar la entrada o descargar. Intenta de nuevo.");
      setStatus("idle");
    }
  };

  const handleRetry = async () => {
    if (!lastPayload) return;
    setInput(lastPayload.input || "");
    // formato fijo
    setIncludeLrc(Boolean(lastPayload.includeLrc));
    await handleDownload();
  };

  return (
    <div className="page">
      <div className="hero">
        <div className="brand">
          <span className="tag">MVP</span>
          <h1>Downtify</h1>
          <p>Pega Spotify, YouTube o un titulo y descargalo en MP3.</p>
        </div>
        <div className="card">
          <label>Spotify / YouTube / Titulo</label>
          <div className="input-row">
            <input
              type="text"
              placeholder="https://open.spotify.com/... o https://youtube.com/... o titulo"
              value={input}
              onChange={(e) => setInput(e.target.value)}
            />
            {canDownload && (
              <button onClick={handleDownload} disabled={status === "downloading"}>
                {status === "downloading" ? "Descargando..." : "Descargar"}
              </button>
            )}
          </div>
          <div className="input-row" style={{ marginTop: 10 }}>
            <div className="format-note">Formato fijo: MP3 (compatibilidad máxima)</div>
          </div>
          <div className="input-row" style={{ marginTop: 10 }}>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={includeLrc}
                onChange={(e) => setIncludeLrc(e.target.checked)}
              />
              Incluir letras (.lrc) si están disponibles
            </label>
          </div>
          {info && <div className="hint">{info}</div>}
          {error && (
            <div className="error">
              {error}
              <div style={{ marginTop: 10 }}>
                <button onClick={handleRetry} disabled={!lastPayload}>
                  Reintentar
                </button>
              </div>
            </div>
          )}
          {meta && (
            <div className="preview">
              {meta.cover_url && (
                <img src={meta.cover_url} alt="Cover" className="cover" />
              )}
              <div className="meta">
                <div className="title">{meta.title}</div>
                {meta.artist && <div className="artist">{meta.artist}</div>}
                {meta.album && <div className="album">{meta.album}</div>}
                {meta.channel && <div className="album">{meta.channel}</div>}
                <div className="type">{meta.media_type || meta.source || "media"}</div>
                {"lyrics_found" in meta && (
                  <div className="album">
                    Letras: {meta.lyrics_found ? `encontradas${meta.lyrics_source ? ` (${meta.lyrics_source})` : ""}` : "no encontradas"}
                  </div>
                )}
              </div>
            </div>
          )}
          {playlistJob && (
            <div className="preview playlist-panel">
              <div className="meta playlist-meta">
                <div className="title">{playlistJob.playlist_title || "Playlist"}</div>
                <div className="album">
                  {playlistJob.done || 0}/{playlistJob.total || 0} completadas
                </div>
                {!!playlistJob.failed && (
                  <div className="error mini-error">
                    Fallidas: {playlistJob.failed}
                  </div>
                )}
                <div className="track-list">
                  {(playlistJob.files || []).map((file) => (
                    <button
                      className="track-download"
                      key={file.id}
                      onClick={async () => {
                        try {
                          await downloadPlaylistTrack(playlistJob.id, file.id, includeLrc);
                        } catch (_) {
                          setError("No pude descargar esa cancion.");
                        }
                      }}
                    >
                      Descargar #{file.index} {file.artist ? `${file.artist} - ` : ""}{file.title}
                      {file.lyrics_found ? " (letras)" : ""}
                    </button>
                  ))}
                </div>
                {playlistJob.ready && (
                  <button
                    className="download"
                    onClick={async () => {
                      try {
                        await downloadPlaylistZip(playlistJob.id);
                      } catch (_) {
                        setError("No pude descargar el ZIP.");
                      }
                    }}
                  >
                    Descargar Todas (ZIP)
                  </button>
                )}
              </div>
            </div>
          )}
          <div className="hint">
            Soporta Spotify/YouTube/titulo. Descarga en MP3 y en playlists puedes bajar una a una o todo en ZIP.
          </div>
        </div>
      </div>
    </div>
  );
}

function getFileName(disposition) {
  if (!disposition) return null;
  const match = /filename="?([^\"]+)"?/i.exec(disposition);
  return match ? match[1] : null;
}

function isSpotifyPlaylistInput(input, meta) {
  if (/spotify\.com\/playlist\//i.test(input || "")) return true;
  return meta?.source === "spotify" && meta?.media_type === "playlist";
}

async function downloadPlaylistTrack(jobId, fileId, includeLrc) {
  const qs = includeLrc ? "?include_lrc=1" : "";
  const res = await fetch(`/api/playlist/file/${jobId}/${fileId}${qs}`);
  if (!res.ok) throw new Error("No se pudo descargar el track");
  const blob = await res.blob();
  const filename = getFileName(res.headers.get("Content-Disposition")) || `track-${fileId}.mp3`;
  const link = document.createElement("a");
  link.href = window.URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function downloadPlaylistZip(jobId) {
  const res = await fetch(`/api/playlist/download/${jobId}`);
  if (!res.ok) throw new Error("No se pudo descargar el ZIP");
  const blob = await res.blob();
  const filename = getFileName(res.headers.get("Content-Disposition")) || "playlist.zip";
  const link = document.createElement("a");
  link.href = window.URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
}
