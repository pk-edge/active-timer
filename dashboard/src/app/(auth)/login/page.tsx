"use client";
import { useState, useEffect, FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { adminLogin, loginWithToken, setPassword, BASE } from "@/lib/api";
import { Clock, Eye, EyeOff, X } from "lucide-react";
import EdgeLogo from "@/components/EdgeLogo";

/* ── Google "G" SVG icon ─────────────────────────────────────────────────── */
function GoogleIcon() {
  return (
    <svg viewBox="0 0 24 24" className="w-5 h-5" aria-hidden="true">
      <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
      <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
      <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z"/>
      <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
    </svg>
  );
}

/* ── Password-reset modal (shown once for new Google users) ──────────────── */
function SetPasswordModal({ onDone }: { onDone: () => void }) {
  const [pw,      setPw]      = useState("");
  const [confirm, setConfirm] = useState("");
  const [show,    setShow]    = useState(false);
  const [error,   setError]   = useState("");
  const [saving,  setSaving]  = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (pw.length < 8) { setError("Password must be at least 8 characters."); return; }
    if (pw !== confirm) { setError("Passwords do not match."); return; }
    setSaving(true);
    try {
      await setPassword(pw);
      onDone();
    } catch {
      setError("Failed to set password. Please try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="w-full max-w-sm mx-4 bg-white rounded-2xl shadow-xl p-8">
        {/* Header */}
        <div className="flex items-start justify-between mb-1">
          <h2 className="text-lg font-semibold text-gray-900">Set up your password</h2>
          <button
            onClick={onDone}
            className="text-gray-400 hover:text-gray-600 transition -mt-1 -mr-1 p-1"
            aria-label="Skip for now"
          >
            <X size={18} />
          </button>
        </div>
        <p className="text-sm text-gray-500 mb-6">
          You&apos;re signed in with Google. Optionally set a password so you can also
          sign in with email.
        </p>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Password */}
          <div>
            <label className="block text-sm text-gray-600 mb-1.5">New password</label>
            <div className="relative">
              <input
                type={show ? "text" : "password"}
                value={pw}
                onChange={e => setPw(e.target.value)}
                required
                minLength={8}
                className="w-full bg-white border border-gray-300 rounded-lg px-3 py-2.5 pr-10 text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
                placeholder="At least 8 characters"
              />
              <button
                type="button"
                onClick={() => setShow(s => !s)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                tabIndex={-1}
              >
                {show ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>

          {/* Confirm */}
          <div>
            <label className="block text-sm text-gray-600 mb-1.5">Confirm password</label>
            <input
              type={show ? "text" : "password"}
              value={confirm}
              onChange={e => setConfirm(e.target.value)}
              required
              className="w-full bg-white border border-gray-300 rounded-lg px-3 py-2.5 text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
              placeholder="Repeat password"
            />
          </div>

          {error && <p className="text-red-500 text-sm">{error}</p>}

          <button
            type="submit"
            disabled={saving}
            className="w-full bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white font-medium py-2.5 rounded-lg text-sm transition"
          >
            {saving ? "Saving…" : "Set password"}
          </button>

          <button
            type="button"
            onClick={onDone}
            className="w-full text-sm text-gray-400 hover:text-gray-600 transition py-1"
          >
            Skip for now
          </button>
        </form>
      </div>
    </div>
  );
}

/* ── Error messages from the OAuth callback ──────────────────────────────── */
const OAUTH_ERRORS: Record<string, string> = {
  domain_not_allowed:    "Only @onedge.co accounts are allowed to sign in with Google.",
  account_inactive:      "Your account is inactive. Contact an administrator.",
  google_token_failed:   "Google sign-in failed. Please try again.",
  google_unreachable:    "Could not reach Google. Check your connection and try again.",
  google_missing_email:  "Google did not return an email address. Try a different account.",
  email_unverified:      "Your Google email is not verified. Verify it with Google and try again.",
  invalid_state:         "OAuth session expired. Please try again.",
  oauth_not_configured:  "Google sign-in is not yet configured. Use email login.",
  company_not_found:     "System not set up yet. Contact an administrator.",
  no_access:             "Your account does not have dashboard access.",
};

/* ── Main login page ─────────────────────────────────────────────────────── */
export default function LoginPage() {
  const router = useRouter();

  const [email,         setEmail]        = useState("");
  const [password,      setPassword_]    = useState("");
  const [error,         setError]        = useState("");
  const [pending,       setPending]      = useState(false);
  const [loading,       setLoading]      = useState(false);
  const [googleLoading, setGoogleLoading] = useState(false);
  const [showPwReset,   setShowPwReset]  = useState(false);

  /* ── Handle redirect from Google OAuth callback ──────────────────────── */
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const token    = params.get("token");
    const newUser  = params.get("new_user") === "true";
    const oauthErr = params.get("error");

    if (oauthErr) {
      setError(OAUTH_ERRORS[oauthErr] ?? "Google sign-in failed. Please try again.");
      // Clean the URL so a refresh doesn't show the error again.
      window.history.replaceState({}, "", "/login");
      return;
    }

    if (token) {
      loginWithToken(token);
      window.history.replaceState({}, "", "/login");
      if (newUser) {
        setShowPwReset(true); // show password-setup modal before navigating
      } else {
        router.replace("/");
      }
    }
  }, [router]);

  /* ── Email + password sign-in ────────────────────────────────────────── */
  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(""); setPending(false);
    setLoading(true);
    try {
      await adminLogin(email, password);
      router.replace("/");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "";
      if (msg.includes("pending admin approval") || msg.includes("403")) {
        setPending(true);
      } else {
        setError("Invalid email or password.");
      }
    } finally {
      setLoading(false);
    }
  }

  /* ── Google sign-in ──────────────────────────────────────────────────── */
  function handleGoogleSignIn() {
    setGoogleLoading(true);
    // Use NEXT_PUBLIC_GOOGLE_OAUTH_BASE when the registered redirect URI in
    // Google Cloud Console differs from NEXT_PUBLIC_API_URL (e.g. LAN IP vs localhost).
    const oauthBase = process.env.NEXT_PUBLIC_GOOGLE_OAUTH_BASE ?? BASE;
    // Tell the backend where to send us back, so it returns to THIS dashboard
    // (staging/prod/local) instead of a hardcoded URL. Allowlisted server-side.
    const origin = encodeURIComponent(window.location.origin);
    window.location.href = `${oauthBase}/v1/auth/google?origin=${origin}`;
  }

  return (
    <>
      {showPwReset && (
        <SetPasswordModal onDone={() => { setShowPwReset(false); router.replace("/"); }} />
      )}

      <div className="min-h-screen flex items-center justify-center p-4 bg-gray-50">
        <div className="w-full max-w-sm">

          {/* Logo */}
          <div className="flex items-center justify-center gap-3 mb-8">
            <EdgeLogo variant="icon" size={40} />
            <span className="text-xl font-semibold text-gray-900">Time Tracker</span>
          </div>

          <div className="bg-white border border-gray-200 rounded-2xl p-8 shadow-sm">
            <h1 className="text-lg font-semibold text-gray-900 mb-6">Sign in</h1>

            {/* ── Google button ─────────────────────────────────────────── */}
            <button
              type="button"
              onClick={handleGoogleSignIn}
              disabled={googleLoading}
              className="w-full flex items-center justify-center gap-3 border border-gray-300 hover:border-gray-400 bg-white hover:bg-gray-50 disabled:opacity-50 text-gray-700 font-medium py-2.5 rounded-lg text-sm transition mb-5"
            >
              <GoogleIcon />
              {googleLoading ? "Redirecting…" : "Continue with Google"}
            </button>

            {/* ── Divider ───────────────────────────────────────────────── */}
            <div className="flex items-center gap-3 mb-5">
              <div className="flex-1 h-px bg-gray-200" />
              <span className="text-xs text-gray-400">or sign in with email</span>
              <div className="flex-1 h-px bg-gray-200" />
            </div>

            {/* ── Email / password form ─────────────────────────────────── */}
            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="block text-sm text-gray-600 mb-1.5">Email</label>
                <input
                  type="email" required value={email}
                  onChange={e => setEmail(e.target.value)}
                  className="w-full bg-white border border-gray-300 rounded-lg px-3 py-2.5 text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
                  placeholder="you@onedge.co"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-600 mb-1.5">Password</label>
                <input
                  type="password" required value={password}
                  onChange={e => setPassword_(e.target.value)}
                  className="w-full bg-white border border-gray-300 rounded-lg px-3 py-2.5 text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition"
                  placeholder="••••••••"
                />
              </div>

              {error && <p className="text-red-500 text-sm">{error}</p>}

              {pending && (
                <div className="flex items-start gap-2.5 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2.5">
                  <Clock size={16} className="text-amber-500 shrink-0 mt-0.5" />
                  <p className="text-sm text-amber-700">
                    Your account is awaiting admin approval. You&apos;ll be able to sign in once approved.
                  </p>
                </div>
              )}

              <button
                type="submit" disabled={loading}
                className="w-full bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white font-medium py-2.5 rounded-lg text-sm transition"
              >
                {loading ? "Signing in…" : "Sign in"}
              </button>
            </form>

            <p className="text-center text-sm text-gray-500 mt-5">
              Don&apos;t have an account?{" "}
              <Link href="/register" className="text-brand-600 hover:text-brand-700 font-medium">
                Request access
              </Link>
            </p>
          </div>
        </div>
      </div>
    </>
  );
}
