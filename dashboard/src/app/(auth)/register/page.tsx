"use client";
import { useState, FormEvent } from "react";
import Link from "next/link";
import { Timer, CheckCircle2 } from "lucide-react";
import { registerUser } from "@/lib/api";

export default function RegisterPage() {
  const [name,     setName]    = useState("");
  const [email,    setEmail]   = useState("");
  const [password, setPassword] = useState("");
  const [confirm,  setConfirm]  = useState("");
  const [error,    setError]   = useState("");
  const [loading,  setLoading] = useState(false);
  const [done,     setDone]    = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setLoading(true);
    try {
      await registerUser({ name: name.trim(), email, password });
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Registration failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4 bg-gray-50">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex items-center justify-center gap-3 mb-8">
          <div className="w-10 h-10 bg-brand-600 rounded-xl flex items-center justify-center">
            <Timer size={20} className="text-white" />
          </div>
          <span className="text-xl font-semibold text-gray-900">TimeTracker</span>
        </div>

        <div className="bg-white border border-gray-200 rounded-2xl p-8 shadow-sm">
          {done ? (
            <div className="text-center">
              <CheckCircle2 size={40} className="mx-auto mb-3 text-green-500" />
              <h1 className="text-lg font-semibold text-gray-900 mb-2">Request submitted!</h1>
              <p className="text-sm text-gray-600 mb-5">
                An administrator will review your account. You&apos;ll be able to sign in once approved.
              </p>
              <Link href="/login"
                    className="block w-full text-center bg-brand-600 hover:bg-brand-700 text-white font-medium py-2.5 rounded-lg text-sm transition">
                Back to sign in
              </Link>
            </div>
          ) : (
            <>
              <h1 className="text-lg font-semibold text-gray-900 mb-6">Create an account</h1>

              <form onSubmit={handleSubmit} className="space-y-4">
                <div>
                  <label className="block text-sm text-gray-600 mb-1.5">Full name</label>
                  <input
                    type="text" required value={name} onChange={e => setName(e.target.value)}
                    placeholder="Your name"
                    className="w-full bg-white border border-gray-300 rounded-lg px-3 py-2.5 text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
                  />
                </div>

                <div>
                  <label className="block text-sm text-gray-600 mb-1.5">Work email</label>
                  <input
                    type="email" required value={email} onChange={e => setEmail(e.target.value)}
                    placeholder="you@company.com"
                    className="w-full bg-white border border-gray-300 rounded-lg px-3 py-2.5 text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
                  />
                </div>

                <div>
                  <label className="block text-sm text-gray-600 mb-1.5">Password</label>
                  <input
                    type="password" required value={password} onChange={e => setPassword(e.target.value)}
                    placeholder="At least 8 characters"
                    className="w-full bg-white border border-gray-300 rounded-lg px-3 py-2.5 text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
                  />
                </div>

                <div>
                  <label className="block text-sm text-gray-600 mb-1.5">Confirm password</label>
                  <input
                    type="password" required value={confirm} onChange={e => setConfirm(e.target.value)}
                    placeholder="••••••••"
                    className="w-full bg-white border border-gray-300 rounded-lg px-3 py-2.5 text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
                  />
                </div>

                {error && <p className="text-red-500 text-sm">{error}</p>}

                <button
                  type="submit" disabled={loading}
                  className="w-full bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white font-medium py-2.5 rounded-lg text-sm transition"
                >
                  {loading ? "Submitting…" : "Request access"}
                </button>
              </form>

              <p className="text-center text-sm text-gray-600 mt-5">
                Already have an account?{" "}
                <Link href="/login" className="text-brand-600 hover:text-brand-700 font-medium">Sign in</Link>
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
