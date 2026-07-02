"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import useSWR from "swr";
import {
  Check, ChevronLeft, ChevronRight, Copy, FolderOpen,
  Loader2, Paperclip, Search, Trash2, Upload,
} from "lucide-react";
import { format, formatDistanceToNow } from "date-fns";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/AccessDenied";
import {
  BASE, deleteSharedFile, getFileLimits, getSharedFiles, uploadSharedFile,
} from "@/lib/api";
import { logEvent } from "@/lib/auditLogger";
import type { SharedFilesPage } from "@/types";

const PAGE_SIZE = 20;

function fmtFileSize(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(Math.floor(Math.log2(bytes) / 10), units.length - 1);
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

function mimeLabel(mime: string | null): string {
  if (!mime) return "";
  const sub = mime.split("/")[1] ?? mime;
  return sub.replace(/^vnd\..*?\+/, "").toUpperCase().slice(0, 8);
}

export default function FilesPage() {
  const { can, loading } = useUser();

  const [search,          setSearch]         = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [page,            setPage]           = useState(0);

  // Debounce search input 300 ms; reset page on new query
  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedSearch(search);
      setPage(0);
    }, 300);
    return () => clearTimeout(t);
  }, [search]);

  const swrKey = can("files", "view")
    ? ["shared-files", page, debouncedSearch]
    : null;

  const { data, mutate, isLoading } = useSWR<SharedFilesPage>(
    swrKey,
    ([, p, s]: [string, number, string]) => getSharedFiles(s || undefined, p * PAGE_SIZE, PAGE_SIZE),
  );

  const { data: limits } = useSWR(
    can("files", "view") ? "file-limits" : null,
    getFileLimits,
  );
  const maxMb = limits?.max_file_size_mb ?? 500;

  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading,   setUploading]   = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [dragOver,    setDragOver]    = useState(false);
  const [copiedId,    setCopiedId]    = useState<string | null>(null);
  const [deletingId,  setDeletingId]  = useState<string | null>(null);

  const handleUpload = useCallback(async (fileList: File[]) => {
    if (fileList.length === 0) return;
    setUploading(true);
    setUploadError(null);
    const maxBytes = maxMb * 1024 * 1024;
    const errors: string[] = [];
    for (const file of fileList) {
      if (file.size > maxBytes) {
        errors.push(`${file.name} exceeds the ${maxMb} MB limit`);
        continue;
      }
      try {
        await uploadSharedFile(file);
        logEvent("file.upload", "shared_file", undefined, { filename: file.name, size_bytes: file.size, mime: file.type });
      } catch (err: unknown) {
        errors.push(`${file.name}: ${err instanceof Error ? err.message : "upload failed"}`);
      }
    }
    await mutate();
    setUploadError(errors.length ? errors.join(" · ") : null);
    setUploading(false);
    if (fileRef.current) fileRef.current.value = "";
  }, [mutate, maxMb]);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const fs = e.target.files;
    if (fs && fs.length) handleUpload(Array.from(fs));
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const fs = e.dataTransfer.files;
    if (fs && fs.length) handleUpload(Array.from(fs));
  };

  const handleCopy = (token: string, id: string) => {
    const url = `${BASE}/v1/files/public/${token}`;
    navigator.clipboard.writeText(url).catch(() => {});
    logEvent("file.copy_link", "shared_file", id);
    setCopiedId(id);
    setTimeout(() => setCopiedId(cur => (cur === id ? null : cur)), 2000);
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this file? The public link will stop working.")) return;
    setDeletingId(id);
    try {
      await deleteSharedFile(id);
      logEvent("file.delete", "shared_file", id);
      await mutate();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setDeletingId(null);
    }
  };

  if (loading) return <div className="p-8 text-sm text-gray-500">Loading…</div>;
  if (!can("files", "view")) return <AccessDenied />;

  const files      = data?.items ?? [];
  const total      = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const from       = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const to         = Math.min((page + 1) * PAGE_SIZE, total);

  return (
    <div className="p-6 space-y-5 max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">Shared Files</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            Upload files and share permanent public download links
          </p>
        </div>
        <div className="flex items-center gap-2">
          {/* Search bar */}
          <div className="relative">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search files…"
              className="pl-8 pr-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 w-52"
            />
          </div>
          {can("files", "create") && (
            <button
              onClick={() => fileRef.current?.click()}
              disabled={uploading}
              className="flex items-center gap-2 px-4 py-2 bg-brand-600 text-white rounded-lg text-sm font-medium hover:bg-brand-700 disabled:opacity-50 transition-colors"
            >
              {uploading
                ? <Loader2 size={15} className="animate-spin" />
                : <Upload size={15} />}
              {uploading ? "Uploading…" : "Upload Files"}
            </button>
          )}
        </div>
      </div>

      <input
        ref={fileRef}
        type="file"
        multiple
        className="hidden"
        onChange={handleFileChange}
      />

      {/* Drop zone */}
      {can("files", "create") && (
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          onClick={() => !uploading && fileRef.current?.click()}
          className={[
            "border-2 border-dashed rounded-xl p-8 text-center transition-colors",
            uploading ? "opacity-50 cursor-not-allowed" : "cursor-pointer",
            dragOver
              ? "border-brand-500 bg-brand-50"
              : "border-gray-200 hover:border-brand-400 hover:bg-gray-50",
          ].join(" ")}
        >
          {uploading ? (
            <Loader2 size={28} className="mx-auto mb-2 text-brand-500 animate-spin" />
          ) : (
            <FolderOpen size={28} className="mx-auto mb-2 text-gray-400" />
          )}
          <p className="text-sm text-gray-600">
            {uploading
              ? "Uploading, please wait…"
              : <>Drag files here or <span className="text-brand-600 font-medium">browse</span></>}
          </p>
          <p className="text-xs text-gray-400 mt-1">Any file type · up to {maxMb} MB · multiple files supported</p>
        </div>
      )}

      {uploadError && (
        <div className="text-sm text-red-700 bg-red-50 border border-red-200 px-4 py-2.5 rounded-lg">
          {uploadError}
        </div>
      )}

      {/* File table */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        {isLoading ? (
          <div className="p-10 text-center text-sm text-gray-400">Loading files…</div>
        ) : files.length === 0 ? (
          <div className="p-10 text-center">
            <Paperclip size={28} className="mx-auto mb-2 text-gray-300" />
            <p className="text-sm text-gray-500">
              {debouncedSearch ? `No files match "${debouncedSearch}"` : "No files uploaded yet"}
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">File</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">Size</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider hidden sm:table-cell">Uploaded By</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider hidden md:table-cell">Date</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">Public Link</th>
                  {can("files", "delete") && <th className="px-4 py-3 w-10" />}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {files.map((f) => (
                  <tr key={f.id} className="hover:bg-gray-50 transition-colors">
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2 min-w-0">
                        <Paperclip size={13} className="text-gray-400 shrink-0" />
                        <span
                          className="font-medium text-gray-800 truncate max-w-[180px]"
                          title={f.original_filename}
                        >
                          {f.original_filename}
                        </span>
                        {f.mime_type && (
                          <span className="hidden lg:inline-flex shrink-0 px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-100 text-gray-500">
                            {mimeLabel(f.mime_type)}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-gray-600 whitespace-nowrap">{fmtFileSize(f.file_size_bytes)}</td>
                    <td className="px-4 py-3 text-gray-600 hidden sm:table-cell">{f.uploaded_by_name ?? "—"}</td>
                    <td className="px-4 py-3 text-gray-500 hidden md:table-cell whitespace-nowrap">
                      {f.created_at ? (
                        <span title={format(new Date(f.created_at), "PPpp")}>
                          {formatDistanceToNow(new Date(f.created_at), { addSuffix: true })}
                        </span>
                      ) : "—"}
                    </td>
                    <td className="px-4 py-3">
                      <button
                        onClick={() => handleCopy(f.public_token, f.id)}
                        className={[
                          "flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors",
                          copiedId === f.id
                            ? "bg-green-100 text-green-700"
                            : "bg-brand-50 text-brand-700 hover:bg-brand-100",
                        ].join(" ")}
                        title={`${BASE}/v1/files/public/${f.public_token}`}
                      >
                        {copiedId === f.id
                          ? <><Check size={11} /> Copied!</>
                          : <><Copy size={11} /> Copy Link</>}
                      </button>
                    </td>
                    {can("files", "delete") && (
                      <td className="px-4 py-3">
                        <button
                          onClick={() => handleDelete(f.id)}
                          disabled={deletingId === f.id}
                          className="p-1.5 rounded-lg text-gray-400 hover:text-red-600 hover:bg-red-50 transition-colors disabled:opacity-40"
                          title="Delete file"
                        >
                          {deletingId === f.id
                            ? <Loader2 size={14} className="animate-spin" />
                            : <Trash2 size={14} />}
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Pagination */}
      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-sm text-gray-500">
          <span>
            Showing {from}–{to} of {total} file{total !== 1 ? "s" : ""}
          </span>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setPage(p => Math.max(0, p - 1))}
              disabled={page === 0}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors text-gray-600"
            >
              <ChevronLeft size={14} /> Previous
            </button>
            <span className="px-3 text-gray-400">
              {page + 1} / {totalPages}
            </span>
            <button
              onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
              disabled={page >= totalPages - 1}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-gray-200 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors text-gray-600"
            >
              Next <ChevronRight size={14} />
            </button>
          </div>
        </div>
      )}

      <p className="text-xs text-gray-400">
        Public links never expire. Anyone with the link can download the file without signing in.
        Delete the file to revoke access.
      </p>
    </div>
  );
}
