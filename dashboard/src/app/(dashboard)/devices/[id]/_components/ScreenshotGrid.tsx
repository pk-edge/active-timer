"use client";
import { fmtTime, fmtBytes } from "@/lib/api";
import type { Screenshot } from "@/types";
import { Image as ImageIcon } from "lucide-react";

interface Props { screenshots: Screenshot[] | undefined }

export default function ScreenshotGrid({ screenshots }: Props) {
  if (!screenshots?.length) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl py-12 text-center shadow-sm">
        <ImageIcon size={32} className="mx-auto mb-3 text-gray-200" />
        <p className="text-gray-600 text-sm">No screenshots yet.</p>
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3">
      {screenshots.map(s => (
        <a
          key={s.id}
          href={s.s3_url}
          target="_blank"
          rel="noreferrer"
          className="block rounded-xl overflow-hidden border border-gray-200 hover:border-brand-300 hover:shadow-md transition group"
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={s.s3_url}
            alt=""
            className="w-full aspect-video object-cover group-hover:opacity-90 transition"
          />
          <div className="px-2 py-1.5 bg-gray-50 border-t border-gray-100 flex items-center justify-between">
            <p className="text-xs text-gray-600">{fmtTime(s.captured_at_unix)}</p>
            <p className="text-xs text-gray-600">{fmtBytes(s.file_size)}</p>
          </div>
        </a>
      ))}
    </div>
  );
}
