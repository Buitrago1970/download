import React, { useEffect, useMemo, useState } from "react";

const API_BASE = "";
const HISTORY_KEY = "downtify_recent_inputs";
const HISTORY_LIMIT = 10;
const QUEUE_WORKERS = 3;

class ApiError extends Error {
  constructor(message, status = 0, detail = "") {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function parseErrorResponse(res, fallback) {
  const raw = await res.text();
  let detail = raw;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && parsed.detail) {
      detail = String(parsed.detail);
    }
  } catch (_) {
    // keep raw text if not JSON
  }
  return new ApiError(detail || fallback, res.status, detail || fallback);
}

async function postJson(path, payload) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    throw await parseErrorResponse(res, "Error de servidor");
  }
  return res.json();
}

export default function App() {
  const [input, setInput] = useState("");
  const [recentInputs, setRecentInputs] = useState([]);
  const [queue, setQueue] = useState([]);
  const [queueFilter, setQueueFilter] = useState("all");
  const [nowTick, setNowTick] = useState(Date.now());
  const [meta, setMeta] = useState(null);
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [toast, setToast] = useState("");
  const [playlistJobId, setPlaylistJobId] = useState("");
  const [playlistJob, setPlaylistJob] = useState(null);
  const readyTrackIds = new Set((playlistJob?.files || []).map((file) => String(file.id)));

  const runningCount = useMemo(
    () => queue.filter((item) => item.status === "resolving" || item.status === "downloading").length,
    [queue]
  );
  const queueStats = useMemo(() => {
    const total = queue.length;
    const queued = queue.filter((item) => item.status === "queued").length;
    const resolving = queue.filter((item) => item.status === "resolving").length;
    const downloading = queue.filter((item) => item.status === "downloading").length;
    const done = queue.filter((item) => item.status === "done").length;
    const failed = queue.filter((item) => item.status === "error").length;
    const completed = done + failed;
    const progress = total ? Math.round((completed / total) * 100) : 0;
    return { total, queued, resolving, downloading, done, failed, progress };
  }, [queue]);
  const activeItem = useMemo(
    () => queue.find((item) => item.status === "downloading" || item.status === "resolving") || null,
    [queue]
  );
  const visibleQueue = useMemo(() => {
    if (queueFilter === "active") {
      return queue.filter((item) => item.status === "queued" || item.status === "resolving" || item.status === "downloading");
    }
    if (queueFilter === "error") {
      return queue.filter((item) => item.status === "error");
    }
    if (queueFilter === "done") {
      return queue.filter((item) => item.status === "done");
    }
    return queue;
  }, [queue, queueFilter]);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(HISTORY_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        setRecentInputs(parsed.filter((item) => typeof item === "string").slice(0, HISTORY_LIMIT));
      }
    } catch (_) {
      // ignore invalid local storage content
    }
  }, []);

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
    const available = Math.max(0, QUEUE_WORKERS - runningCount);
    if (available === 0) return;
    const pending = queue.filter((item) => item.status === "queued").slice(0, available);
    if (!pending.length) return;
    pending.forEach((item) => {
      processQueueItem(item).catch(() => {
        // queue item state handled inside processQueueItem
      });
    });
  }, [queue, runningCount]);

  useEffect(() => {
    if (runningCount === 0) return undefined;
    const t = setInterval(() => setNowTick(Date.now()), 1000);
    return () => clearInterval(t);
  }, [runningCount]);

  useEffect(() => {
    const message = error || info;
    if (!message) return undefined;
    setToast(message);
    const t = setTimeout(() => setToast(""), 3500);
    return () => clearTimeout(t);
  }, [error, info]);

  const handlePasteAndDownload = () => {
    const lines = splitBatchInput(input);
    if (!lines.length) return;
    setError("");
    setInfo(`${lines.length} item(s) agregados a la cola.`);
    addBatchToHistory(lines);
    enqueueMany(lines);
    setInput("");
  };

  const processQueueItem = async (item) => {
    updateQueueItem(item.id, { status: "resolving", error: "", startedAt: Date.now(), phase: "Resolviendo..." });
    setStatus("loading");

    try {
      const data = await postJson("/api/preview", { input: item.input });
      setMeta(data);

      if (isSpotifyPlaylistInput(item.input, data)) {
        setPlaylistJobId("");
        setPlaylistJob(null);
        const started = await postJson("/api/playlist/start", { input: item.input });
        const jobId = started.job_id;
        if (!jobId) throw new ApiError("No se pudo iniciar el job de playlist");
        setPlaylistJobId(jobId);
        setInfo("Playlist en proceso. Puedes descargar canciones en cuanto aparezcan abajo.");
        updateQueueItem(item.id, { status: "done", note: "Playlist iniciada", endedAt: Date.now(), phase: "Completado" });
        setStatus("ready");
        return;
      }

      updateQueueItem(item.id, { status: "downloading", phase: "Descargando archivo..." });
      setStatus("downloading");
      const res = await fetch(`/api/download`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: item.input })
      });
      if (!res.ok) {
        throw await parseErrorResponse(res, "Error en descarga");
      }
      const blob = await res.blob();
      const filename = getFileName(res.headers.get("Content-Disposition")) || "download.mp3";
      const link = document.createElement("a");
      link.href = window.URL.createObjectURL(blob);
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      updateQueueItem(item.id, { status: "done", note: filename, endedAt: Date.now(), phase: "Completado" });
      setStatus("done");
    } catch (err) {
      const friendlyError = mapApiError(err);
      updateQueueItem(item.id, { status: "error", error: friendlyError, endedAt: Date.now(), phase: "Error" });
      setError(`Error en cola: ${friendlyError}`);
      setStatus("ready");
    }
  };

  const addBatchToHistory = (values) => {
    setRecentInputs((prev) => {
      const merged = [...values, ...prev].filter((item, idx, arr) => arr.indexOf(item) === idx).slice(0, HISTORY_LIMIT);
      window.localStorage.setItem(HISTORY_KEY, JSON.stringify(merged));
      return merged;
    });
  };

  const enqueueMany = (values) => {
    const items = values.map((value) => ({
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      input: value,
      status: "queued",
      error: "",
      note: "",
      phase: "En cola",
      createdAt: Date.now(),
      startedAt: null,
      endedAt: null
    }));
    setQueue((prev) => [...prev, ...items]);
  };

  const updateQueueItem = (id, patch) => {
    setQueue((prev) => prev.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  };

  const retryQueueItem = (id) => {
    updateQueueItem(id, { status: "queued", error: "", note: "", phase: "En cola", createdAt: Date.now(), startedAt: null, endedAt: null });
    setError("");
  };

  const retryFailedQueueItems = () => {
    const failedItems = queue.filter((item) => item.status === "error");
    if (!failedItems.length) return;
    setQueue((prev) =>
      prev.map((item) =>
        item.status === "error"
          ? { ...item, status: "queued", error: "", note: "", phase: "En cola", createdAt: Date.now(), startedAt: null, endedAt: null }
          : item
      )
    );
    setInfo(`Reintentando ${failedItems.length} item(s).`);
  };

  const clearCompletedQueueItems = () => {
    const completed = queue.filter((item) => item.status === "done" || item.status === "error").length;
    if (!completed) return;
    setQueue((prev) => prev.filter((item) => item.status !== "done" && item.status !== "error"));
    setInfo(`Se limpiaron ${completed} item(s) completados.`);
  };

  return (
    <div className="page">
      <div className="hero">
        <div className="brand">
          <span className="tag">MVP</span>
          <h1>Downtify</h1>
          <p>Convierte links o texto en audio MP3, rapido y sin pasos extra.</p>
        </div>
        <div className="card">
          {!!toast && <div className="toast" aria-live="polite">{toast}</div>}
          <label>Spotify / YouTube / titulo (una o varias lineas)</label>
          <div className="input-row">
            <textarea
              className="input-field"
              rows={3}
              placeholder={"https://open.spotify.com/...\nhttps://youtube.com/...\nartista - cancion"}
              value={input}
              onChange={(e) => setInput(e.target.value)}
            />
            <button onClick={handlePasteAndDownload} disabled={!input.trim()}>
              Pegar y Descargar
            </button>
          </div>
          {!!recentInputs.length && (
            <div className="history">
              {recentInputs.map((value) => (
                <button key={value} className="history-item" onClick={() => setInput(value)}>
                  {value}
                </button>
              ))}
            </div>
          )}
          {info && <div className="hint">{info}</div>}
          {error && <div className="error">{error}</div>}
          {meta && (
            <div className="preview">
              {meta.cover_url && <img src={meta.cover_url} alt="Cover" className="cover" />}
              <div className="meta">
                <div className="title">{meta.title}</div>
                {meta.artist && <div className="artist">{meta.artist}</div>}
                {meta.album && <div className="album">{meta.album}</div>}
                {meta.channel && <div className="album">{meta.channel}</div>}
                <div className="type">{meta.media_type || meta.source || "media"}</div>
              </div>
            </div>
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
                <div className="playlist-list">
                  {(playlistJob.tracks || []).map((track) => {
                    const isReady = readyTrackIds.has(String(track.id));
                    return (
                      <button
                        key={track.id}
                        className={`playlist-track ${isReady ? "ready" : "pending"}`}
                        disabled={!isReady}
                        onClick={async () => {
                          try {
                            await downloadPlaylistTrack(playlistJob.id, track.id);
                          } catch (_) {
                            setError("No pude descargar esa cancion.");
                          }
                        }}
                      >
                        #{track.index} {track.artist ? `${track.artist} - ` : ""}{track.title}
                        {!isReady && <span className="track-state">Procesando...</span>}
                        {isReady && <span className="track-state">Lista para descargar</span>}
                      </button>
                    );
                  })}
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
          {!!queue.length && (
            <div className="queue">
              <div className="queue-title">Cola de descargas</div>
              <div className="queue-live" aria-live="polite">
                {runningCount > 0 && activeItem
                  ? `Procesando: ${activeItem.input}`
                  : queueStats.queued > 0
                    ? "Esperando siguiente descarga..."
                    : "Cola en espera"}
              </div>
              <div className="queue-stats">
                <div className="queue-stats-top">
                  <span>{queueStats.done + queueStats.failed}/{queueStats.total} completados</span>
                  <span>{queueStats.progress}%</span>
                </div>
                <div className="queue-progress">
                  <div className="queue-progress-fill" style={{ width: `${queueStats.progress}%` }} />
                </div>
                <div className="queue-stats-grid">
                  <span>Activos: {queueStats.resolving + queueStats.downloading}/{QUEUE_WORKERS}</span>
                  <span>Pendientes: {queueStats.queued}</span>
                  <span>OK: {queueStats.done}</span>
                  <span>Error: {queueStats.failed}</span>
                </div>
              </div>
              <div className="queue-toolbar">
                <div className="queue-filters">
                  <button className={`chip ${queueFilter === "all" ? "active" : ""}`} onClick={() => setQueueFilter("all")}>Todo</button>
                  <button className={`chip ${queueFilter === "active" ? "active" : ""}`} onClick={() => setQueueFilter("active")}>Activos</button>
                  <button className={`chip ${queueFilter === "error" ? "active" : ""}`} onClick={() => setQueueFilter("error")}>Errores</button>
                  <button className={`chip ${queueFilter === "done" ? "active" : ""}`} onClick={() => setQueueFilter("done")}>Completados</button>
                </div>
                <div className="queue-actions">
                  <button className="ghost" onClick={retryFailedQueueItems} disabled={queueStats.failed === 0}>Reintentar fallidas</button>
                  <button className="ghost" onClick={clearCompletedQueueItems} disabled={queueStats.done + queueStats.failed === 0}>Limpiar completadas</button>
                </div>
              </div>
              <div className="queue-list">
                {visibleQueue.map((item) => (
                  <div key={item.id} className={`queue-item ${item.status}`}>
                    <div className="queue-text">
                      <div className="queue-input">{item.input}</div>
                      <div className="queue-note">{queueStatusLabel(item, nowTick)}</div>
                    </div>
                    {item.status === "error" && (
                      <button className="retry" onClick={() => retryQueueItem(item.id)}>
                        Reintentar
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="hint">
            Puedes pegar varias lineas y la app procesa hasta {QUEUE_WORKERS} items en paralelo.
          </div>
        </div>
      </div>
    </div>
  );
}

function splitBatchInput(raw) {
  return (raw || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function getFileName(disposition) {
  if (!disposition) return null;
  const match = /filename="?([^\"]+)"?/i.exec(disposition);
  return match ? match[1] : null;
}

function queueStatusLabel(item, nowTick) {
  const start = item.startedAt || item.createdAt;
  const end = item.endedAt || nowTick;
  const elapsed = start ? formatElapsed(end - start) : "";

  if (item.status === "queued") return "En cola";
  if (item.status === "resolving") return `Resolviendo metadata... ${elapsed}`;
  if (item.status === "downloading") return `Descargando... ${elapsed}`;
  if (item.status === "done") return `${item.note || "Completado"}${elapsed ? ` · ${elapsed}` : ""}`;
  return `${item.error || "Error"}${elapsed ? ` · ${elapsed}` : ""}`;
}

function formatElapsed(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes > 0) {
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }
  return `${seconds}s`;
}

function mapApiError(err) {
  const detail = String(err?.detail || err?.message || "Error desconocido");
  const normalized = detail.toLowerCase();
  if (normalized.includes("no youtube match found")) return "No encontre coincidencia en YouTube.";
  if (normalized.includes("playlist page is not accessible")) return "La playlist parece privada o no accesible.";
  if (normalized.includes("playlist job not found")) return "El job de playlist ya no existe.";
  if (normalized.includes("youtube download failed")) return "YouTube rechazo la descarga. Intenta de nuevo o usa cookies.";
  if (normalized.includes("missing input")) return "Entrada vacia. Pega un link o titulo.";
  if (normalized.includes("timed out")) return "Tiempo de espera agotado. Reintenta.";
  return detail.replace(/^error:\s*/i, "");
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
