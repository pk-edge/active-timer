"use client";
import { redirect } from "next/navigation";

// Root "/" redirects to /devices
export default function RootPage() {
  redirect("/devices");
}
