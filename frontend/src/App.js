import { useEffect, useState } from "react";
import "@/App.css";
import axios from "axios";
import { motion } from "framer-motion";
import {
  ArrowRightLeft,
  ShieldCheck,
  Zap,
  Fuel,
  Layers,
  Send,
  Wallet,
  QrCode,
  CheckCircle2,
  Bot,
  Copy,
  Check,
} from "lucide-react";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const fade = {
  hidden: { opacity: 0, y: 24 },
  show: (i = 0) => ({
    opacity: 1,
    y: 0,
    transition: { delay: i * 0.08, duration: 0.5, ease: [0.22, 1, 0.36, 1] },
  }),
};

function App() {
  const [bot, setBot] = useState({ username: null, link: null, name: null });
  const [stats, setStats] = useState({ total_swaps: 0, completed: 0 });
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    axios.get(`${API}/bot-info`).then((r) => setBot(r.data)).catch(() => {});
    axios.get(`${API}/stats`).then((r) => setStats(r.data)).catch(() => {});
  }, []);

  const botLink = bot.link || "https://t.me";
  const botHandle = bot.username ? `@${bot.username}` : "@your_bot";
  const qrSrc = `https://api.qrserver.com/v1/create-qr-code/?size=220x220&margin=1&bgcolor=0C1017&color=FFFFFF&data=${encodeURIComponent(
    botLink
  )}`;

  const copyHandle = () => {
    navigator.clipboard.writeText(botLink);
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
  };

  const steps = [
    {
      icon: Send,
      title: "Open the bot & tap /bridge",
      body: "Enter your Starknet address once — the bot saves it for next time.",
    },
    {
      icon: QrCode,
      title: "Get a fresh deposit address",
      body: "A brand-new, single-use Base address + QR is generated for every bridge.",
    },
    {
      icon: Wallet,
      title: "Send USDC on Base",
      body: "Send from any wallet. The bot detects the deposit automatically.",
    },
    {
      icon: CheckCircle2,
      title: "Receive on Starknet",
      body: "Funds are auto-bridged and delivered to your Starknet address. Gasless to receive.",
    },
  ];

  const features = [
    {
      icon: ShieldCheck,
      title: "Privacy by default",
      body: "A new deposit address every time means your wallets are never reused or linked on-chain.",
      color: "#10B981",
    },
    {
      icon: Fuel,
      title: "Lowest-fee route",
      body: "Your only cost is one Base transfer. Receiving on Starknet is completely gasless.",
      color: "#38BDF8",
    },
    {
      icon: Zap,
      title: "Auto & fast",
      body: "Powered by NEAR Intents. Typical settlement in ~40 seconds after deposit.",
      color: "#F59E0B",
    },
    {
      icon: Layers,
      title: "Non-custodial routing",
      body: "Funds flow through bridge liquidity — no direct A→B trail between your wallets.",
      color: "#7D52F4",
    },
  ];

  return (
    <div className="page" data-testid="landing-page">
      <div className="glow glow-base" />
      <div className="glow glow-star" />

      {/* NAV */}
      <nav className="nav">
        <div className="brand">
          <span className="brand-mark">
            <ArrowRightLeft size={18} strokeWidth={2.5} />
          </span>
          <span className="brand-name">CipherSwap</span>
        </div>
        <a
          href={botLink}
          target="_blank"
          rel="noopener noreferrer"
          className="nav-cta"
          data-testid="nav-open-telegram"
        >
          <Send size={15} /> Open Bot
        </a>
      </nav>

      {/* HERO */}
      <header className="hero">
        <motion.div className="hero-left" initial="hidden" animate="show">
          <motion.div variants={fade} custom={0} className="pill-row">
            <span className="pill pill-base">35 Networks</span>
            <ArrowRightLeft size={13} className="pill-arrow" />
            <span className="pill pill-star">196 Assets</span>
          </motion.div>

          <motion.h1 variants={fade} custom={1} className="hero-title">
            Swap <span className="c-usdc">any coin</span>,
            <br />
            across <span className="c-star">any chain</span>.
            <br />
            Privately.
          </motion.h1>

          <motion.p variants={fade} custom={2} className="hero-sub">
            A privacy-first Telegram bot for cross-chain swaps across 35 networks. Just
            type <em>"swap 5 USDC on base to USDT on bsc"</em> — with fresh addresses,
            amount-blending, splitting and zero-trace mode built in.
          </motion.p>

          <motion.div variants={fade} custom={3} className="hero-actions">
            <a
              href={botLink}
              target="_blank"
              rel="noopener noreferrer"
              className="btn-primary"
              data-testid="hero-open-telegram"
            >
              <Send size={17} /> Launch on Telegram
            </a>
            <button className="btn-ghost" onClick={copyHandle} data-testid="copy-bot-link">
              {copied ? <Check size={16} /> : <Copy size={16} />}
              {copied ? "Copied!" : botHandle}
            </button>
          </motion.div>

          <motion.div variants={fade} custom={4} className="stat-row">
            <div className="stat">
              <div className="stat-num">{stats.total_swaps}</div>
              <div className="stat-label">Bridges Started</div>
            </div>
            <div className="stat-div" />
            <div className="stat">
              <div className="stat-num">~40s</div>
              <div className="stat-label">Avg Settlement</div>
            </div>
            <div className="stat-div" />
            <div className="stat">
              <div className="stat-num">100%</div>
              <div className="stat-label">Gasless Receive</div>
            </div>
          </motion.div>
        </motion.div>

        <motion.div
          className="hero-right"
          initial={{ opacity: 0, scale: 0.94 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.6, delay: 0.2 }}
        >
          <div className="qr-card" data-testid="bot-qr-card">
            <div className="qr-card-top">
              <span className="qr-live">
                <span className="live-dot" /> Bot Online
              </span>
              <Bot size={18} className="qr-bot-icon" />
            </div>
            <div className="qr-wrap">
              <img src={qrSrc} alt="Scan to open bot" className="qr-img" />
            </div>
            <div className="qr-handle">{botHandle}</div>
            <div className="qr-hint">Scan with your phone to start bridging</div>
            <a
              href={botLink}
              target="_blank"
              rel="noopener noreferrer"
              className="qr-btn"
              data-testid="qr-open-telegram"
            >
              <Send size={15} /> Open in Telegram
            </a>
          </div>
        </motion.div>
      </header>

      {/* HOW IT WORKS */}
      <section className="section">
        <div className="section-head">
          <span className="eyebrow">How it works</span>
          <h2 className="section-title">Four taps to a cross-chain transfer</h2>
        </div>
        <div className="steps-grid">
          {steps.map((s, i) => (
            <motion.div
              key={s.title}
              className="step-card"
              custom={i}
              variants={fade}
              initial="hidden"
              whileInView="show"
              viewport={{ once: true, margin: "-60px" }}
              data-testid={`step-card-${i}`}
            >
              <div className="step-index">{String(i + 1).padStart(2, "0")}</div>
              <div className="step-icon">
                <s.icon size={20} strokeWidth={2} />
              </div>
              <h3 className="step-title">{s.title}</h3>
              <p className="step-body">{s.body}</p>
            </motion.div>
          ))}
        </div>
      </section>

      {/* FEATURES */}
      <section className="section">
        <div className="section-head">
          <span className="eyebrow">Why RelayBridge</span>
          <h2 className="section-title">Built for privacy and low fees</h2>
        </div>
        <div className="feat-grid">
          {features.map((f, i) => (
            <motion.div
              key={f.title}
              className="feat-card"
              custom={i}
              variants={fade}
              initial="hidden"
              whileInView="show"
              viewport={{ once: true, margin: "-60px" }}
              data-testid={`feature-card-${i}`}
            >
              <div className="feat-icon" style={{ color: f.color, background: `${f.color}18` }}>
                <f.icon size={22} strokeWidth={2} />
              </div>
              <h3 className="feat-title">{f.title}</h3>
              <p className="feat-body">{f.body}</p>
            </motion.div>
          ))}
        </div>
      </section>

      {/* PRIVACY NOTE */}
      <section className="privacy-band" data-testid="privacy-band">
        <ShieldCheck size={28} className="pb-icon" />
        <div>
          <h3 className="pb-title">Honest about privacy</h3>
          <p className="pb-body">
            Fresh addresses and bridge routing give you strong on-chain hygiene, but no
            bridge can make transactions 100% untraceable. For maximum privacy, use a new
            Starknet address per bridge and wipe your data any time with{" "}
            <code>/forget</code>.
          </p>
        </div>
      </section>

      {/* FINAL CTA */}
      <section className="final-cta">
        <h2 className="final-title">Ready to bridge?</h2>
        <p className="final-sub">Open the bot and send <code>/bridge</code> to begin.</p>
        <a
          href={botLink}
          target="_blank"
          rel="noopener noreferrer"
          className="btn-primary btn-lg"
          data-testid="final-open-telegram"
        >
          <Send size={18} /> Launch {botHandle}
        </a>
      </section>

      <footer className="footer">
        <span>CipherSwap · Any coin, any chain</span>
        <span className="footer-muted">Powered by NEAR Intents · Non-custodial</span>
      </footer>
    </div>
  );
}

export default App;
