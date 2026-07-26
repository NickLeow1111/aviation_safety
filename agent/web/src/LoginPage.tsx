import { useState } from "react";

type LoginPageProps = {
  onLogin: (user: { name: string; role: string }) => void;
};

const DEMO_CREDENTIALS = [
  { name: "Analyst", role: "Safety Analyst", email: "analyst@caas.gov.sg" },
  { name: "Inspector", role: "Safety Inspector", email: "inspector@caas.gov.sg" },
  { name: "Admin", role: "System Admin", email: "admin@caas.gov.sg" },
];

export function LoginPage({ onLogin }: LoginPageProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showDemo, setShowDemo] = useState(false);
  const [error, setError] = useState("");
  const [imgLoaded, setImgLoaded] = useState(true);
  const [imgError, setImgError] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (!email.trim() || !password.trim()) {
      setError("Please enter your credentials.");
      return;
    }

    // Fake auth — accept anything for demo
    onLogin({ name: email.split("@")[0], role: "Safety Analyst" });
  };

  const handleDemoLogin = (user: (typeof DEMO_CREDENTIALS)[number]) => {
    onLogin({ name: user.name, role: user.role });
  };

  return (
    <div className="login-page">
      <div className="login-split">
        {/* Left — Hero image placeholder */}
        <div className="login-hero">
          <div className="login-hero-content">
            <div className="login-hero-image-frame">
              {/* 
                ─────────────────────────────────────────────
                IMAGE PLACEHOLDER
                Upload your landing page graphic to:
                  agent/web/public/login-hero.png
                or
                  agent/static/login-hero.png
                Then replace the src below.
                ─────────────────────────────────────────────
              */}
              <img
                src="/login-hero.png"
                alt="Aviation Safety"
                className="login-hero-img"
                style={{ display: imgError ? "none" : "block" }}
                onError={() => setImgError(true)}
                onLoad={() => setImgError(false)}
              />
              <div className="login-hero-placeholder" style={{ display: imgError ? "flex" : "none" }}>
                <div className="login-hero-placeholder-inner">
                  <svg width="64" height="64" viewBox="0 0 64 64" fill="none">
                    <rect x="8" y="8" width="48" height="48" rx="12" stroke="#1B2A4A" strokeWidth="2" strokeDasharray="4 4" fill="none" opacity="0.4"/>
                    <path d="M32 24v16M24 32h16" stroke="#1B2A4A" strokeWidth="2" strokeLinecap="round" opacity="0.4"/>
                    <path d="M20 44l8-12 6 8 6-12 8 16" stroke="#1B2A4A" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none" opacity="0.3"/>
                    <circle cx="26" cy="26" r="4" stroke="#1B2A4A" strokeWidth="2" opacity="0.3"/>
                  </svg>
                  <p className="login-hero-placeholder-text">
                    Add your landing page graphic
                  </p>
                  <p className="login-hero-placeholder-hint">
                    Save as <code>agent/web/public/login-hero.png</code><br />
                    Recommended: 800×600px
                  </p>
                </div>
              </div>
            </div>
            <div className="login-hero-text">
              <h1>Safety Intelligence Bot</h1>
              <p>AI-powered safety analysis for the Civil Aviation Authority of Singapore</p>
            </div>
          </div>
        </div>

        {/* Right — Login panel */}
        <div className="login-panel">
          <div className="login-panel-inner">
            {/* Logo + branding */}
            <div className="login-panel-brand">
              <div className="login-logo">
                <svg width="36" height="36" viewBox="0 0 40 40" fill="none">
                  <rect width="40" height="40" rx="10" fill="#1B2A4A" />
                  <path d="M20 8C13.373 8 8 13.373 8 20s5.373 12 12 12 12-5.373 12-12S26.627 8 20 8zm0 22c-5.523 0-10-4.477-10-10S14.477 10 20 10s10 4.477 10 10-4.477 10-10 10z" fill="#fff" opacity="0.9"/>
                  <path d="M20 14l-4 6h8l-4 6" stroke="#fff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/>
                  <circle cx="20" cy="20" r="3" fill="#fff" opacity="0.9"/>
                </svg>
              </div>
              <div>
                <h2 className="login-panel-title">Welcome back</h2>
                <p className="login-panel-subtitle">Sign in to your account</p>
              </div>
            </div>

            {/* Login form */}
            <form onSubmit={handleSubmit} className="login-form">
              <div className="login-field">
                <label htmlFor="email">Email</label>
                <input
                  id="email"
                  type="email"
                  placeholder="you@caas.gov.sg"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  autoFocus
                />
              </div>

              <div className="login-field">
                <label htmlFor="password">Password</label>
                <input
                  id="password"
                  type="password"
                  placeholder="••••••••"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>

              {error && <p className="login-error">{error}</p>}

              <button type="submit" className="login-btn">
                Sign In
              </button>
            </form>

            <div className="login-divider">
              <span>or</span>
            </div>

            <button
              className="login-btn login-btn--demo"
              onClick={() => setShowDemo(!showDemo)}
            >
              {showDemo ? "Hide Demo Accounts" : "Demo Login"}
            </button>

            {showDemo && (
              <div className="login-demo-users">
                {DEMO_CREDENTIALS.map((user) => (
                  <button
                    key={user.email}
                    className="login-demo-user"
                    onClick={() => handleDemoLogin(user)}
                  >
                    <div className="login-demo-avatar">
                      {user.name.charAt(0)}
                    </div>
                    <div className="login-demo-info">
                      <span className="login-demo-name">{user.name}</span>
                      <span className="login-demo-role">{user.role}</span>
                    </div>
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                      <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                    </svg>
                  </button>
                ))}
              </div>
            )}

            <p className="login-panel-footer">
              Secure platform for authorised CAAS personnel only.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
