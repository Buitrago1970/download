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
  const [format, setFormat] = useState("best");
  const [meta, setMeta] = useState(null);
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [playlistJobId, setPlaylistJobId] = useState("");
  const [playlistJob, setPlaylistJob] = useState(null);

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

  const handlePreview = async () => {
    setError("");
    setInfo("");
    setPlaylistJobId("");
    setPlaylistJob(null);
    setStatus("loading");
    try {
      const data = await postJson("/api/preview", { input });
      setMeta(data);
      setStatus("ready");
    } catch (err) {
      setError("No pude resolver la entrada. Usa link de Spotify, YouTube o un titulo.");
      setStatus("idle");
    }
  };

  const handleDownload = async () => {
    setError("");
    setInfo("");
    setStatus("downloading");
    try {
      if (isSpotifyPlaylistInput(input, meta)) {
        const started = await postJson("/api/playlist/start", { input, format });
        const jobId = started.job_id;
        if (!jobId) throw new Error("No se pudo iniciar el job de playlist");
        setPlaylistJobId(jobId);
        setInfo("Playlist en proceso. Puedes descargar canciones en cuanto aparezcan abajo.");
        return;
      }

      const res = await fetch(`/api/download`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input, format })
      });
      if (!res.ok) {
        throw new Error("Error en descarga");
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
      setError("No pude descargar. Intenta de nuevo.");
      setStatus("ready");
    }
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
            <button onClick={handlePreview} disabled={!input || status === "loading"}>
              {status === "loading" ? "Buscando..." : "Buscar"}
            </button>
          </div>
          <div className="input-row" style={{ marginTop: 10 }}>
            <select value={format} onChange={(e) => setFormat(e.target.value)}>
              <option value="best">Mejor calidad (sin recodificar)</option>
              <option value="m4a">M4A (AAC original)</option>
              <option value="opus">Opus (alta calidad)</option>
              <option value="mp3">MP3 (compatibilidad)</option>
            </select>
          </div>
          {info && <div className="hint">{info}</div>}
          {error && <div className="error">{error}</div>}
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
              </div>
            </div>
          )}
          {meta && (
            <button className="download" onClick={handleDownload} disabled={status === "downloading"}>
              {status === "downloading" ? "Descargando..." : isSpotifyPlaylistInput(input, meta) ? "Descargar Playlist (ZIP)" : "Descargar MP3"}
            </button>
          )}
          {playlistJob && (
            <div className="preview">
              <div className="meta" style={{ width: "100%" }}>
                <div className="title">{playlistJob.playlist_title || "Playlist"}</div>
                <div className="album">
                  {playlistJob.done || 0}/{playlistJob.total || 0} completadas
                </div>
                {!!playlistJob.failed && (
                  <div className="error" style={{ marginTop: 8 }}>
                    Fallidas: {playlistJob.failed}
                  </div>
                )}
                <div style={{ marginTop: 10, display: "grid", gap: 8 }}>
                  {(playlistJob.files || []).map((file) => (
                    <button
                      key={file.id}
                      onClick={async () => {
                        try {
                          await downloadPlaylistTrack(playlistJob.id, file.id);
                        } catch (_) {
                          setError("No pude descargar esa cancion.");
                        }
                      }}
                    >
                      Descargar #{file.index} {file.artist ? `${file.artist} - ` : ""}{file.title}
                    </button>
                  ))}
                </div>
                {playlistJob.ready && (
                  <button
                    className="download"
                    style={{ marginTop: 10 }}
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
            Soporta Spotify/YouTube/titulo. Elige formato (best/m4a/opus/mp3) y en playlists puedes bajar una a una o todo en ZIP.
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

async function downloadPlaylistTrack(jobId, fileId) {
  const res = await fetch(`/api/playlist/file/${jobId}/${fileId}`);
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
