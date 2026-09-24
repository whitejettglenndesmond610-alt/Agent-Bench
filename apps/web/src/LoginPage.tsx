import { FormEvent, useState } from "react";

import { SessionRole } from "./api";
import "./login.css";

interface LoginPageProps {
  error?: string;
  pending: boolean;
  onLogin: (role: SessionRole, password: string) => Promise<void>;
  onUserLogin: (username: string, password: string) => Promise<void>;
  onResetError: () => void;
}

const roles: Array<{ value: SessionRole; label: string; description: string }> = [
  { value: "read_only", label: "普通用户", description: "在线运行 Agent 并查看排行榜" },
  { value: "operator", label: "评测管理员", description: "管理 Agent、实验与正式评测" }
];

export function LoginPage({ error, pending, onLogin, onUserLogin, onResetError }: LoginPageProps) {
  const [mode, setMode] = useState<"role" | "account">("role");
  const [role, setRole] = useState<SessionRole>("read_only");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (mode === "account") {
      await onUserLogin(username, password);
    } else {
      await onLogin(role, password);
    }
  }

  function selectRole(nextRole: SessionRole) {
    setRole(nextRole);
    onResetError();
  }

  function selectMode(nextMode: "role" | "account") {
    setMode(nextMode);
    setPassword("");
    onResetError();
  }

  return (
    <main className="login-shell grain">
      <section className="login-card">
        <header className="login-card-heading">
          <div className="brand login-brand">
            <span className="brand-glyph">AB</span>
            <div><strong>Agent Bench</strong><small>Internal evaluation</small></div>
          </div>
          <h1>统一评测，让选择有据可依。</h1>
          <p className="login-intro">
            第三方 Agent 评测与排行榜平台：正式评测、在线运行、多 Agent 对比与精选案例策展，一站完成。
          </p>
        </header>

        <form className="login-form" onSubmit={submit}>
          <div className="login-form-heading">
            <span>01 / AUTHENTICATE</span>
            <h2>{mode === "role" ? "选择访问身份" : "登录个人账号"}</h2>
          </div>

          <div className="login-mode-switch" aria-label="登录方式">
            <button type="button" aria-pressed={mode === "role"} onClick={() => selectMode("role")}>角色密码</button>
            <button type="button" aria-pressed={mode === "account"} onClick={() => selectMode("account")}>个人账号</button>
          </div>

          {mode === "role" ? (
            <div className="role-grid" aria-label="访问身份">
              {roles.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  className={`role-option ${role === item.value ? "is-selected" : ""}`}
                  aria-pressed={role === item.value}
                  onClick={() => selectRole(item.value)}
                >
                  <span>{item.label}</span>
                  <small>{item.description}</small>
                </button>
              ))}
            </div>
          ) : (
            <label className="password-field account-username-field">
              <span>用户名</span>
              <input
                type="text"
                name="username"
                autoComplete="username"
                value={username}
                onChange={(event) => {
                  setUsername(event.target.value);
                  onResetError();
                }}
                placeholder="输入个人用户名"
                required
                autoFocus
              />
            </label>
          )}

          <label className="password-field">
            <span>{mode === "role" ? "共享密码" : "账号密码"}</span>
            <input
              type="password"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
                onResetError();
              }}
              placeholder={mode === "role" ? "输入内部访问密码" : "输入账号密码"}
              required
              autoFocus={mode === "role"}
            />
          </label>

          <div className={`login-error ${error ? "is-visible" : ""}`} role="alert">
            {error ?? "占位"}
          </div>

          <button className="login-submit" type="submit" disabled={pending || !password || (mode === "account" && !username.trim())}>
            <span>{pending ? "正在验证" : "进入平台"}</span>
            <span aria-hidden="true">-&gt;</span>
          </button>

          <p className="session-note">
            {mode === "account"
              ? "个人账号可访问已授权的 Agent 在线运行"
              : role === "operator"
                ? "评测管理会话将在 8 小时后失效"
                : "普通用户会话将在 7 天后失效"}
          </p>
        </form>

        <footer className="login-signal"><span />INTERNAL ONLY</footer>
      </section>
    </main>
  );
}
