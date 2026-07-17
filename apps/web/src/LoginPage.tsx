import { FormEvent, useState } from "react";

import { SessionRole } from "./api";

interface LoginPageProps {
  error?: string;
  pending: boolean;
  onLogin: (role: SessionRole, password: string) => Promise<void>;
  onResetError: () => void;
}

const roles: Array<{ value: SessionRole; label: string; description: string }> = [
  { value: "read_only", label: "只读访问", description: "查看榜单和对比报告" },
  { value: "operator", label: "运营访问", description: "管理 Agent 并发起评测" }
];

export function LoginPage({ error, pending, onLogin, onResetError }: LoginPageProps) {
  const [role, setRole] = useState<SessionRole>("read_only");
  const [password, setPassword] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await onLogin(role, password);
  }

  function selectRole(nextRole: SessionRole) {
    setRole(nextRole);
    onResetError();
  }

  return (
    <main className="login-shell">
      <section className="login-context">
        <div className="brand login-brand">
          <span className="brand-glyph">AB</span>
          <div><strong>Agent Bench</strong><small>Internal evaluation</small></div>
        </div>
        <div>
          <p className="eyebrow">CONTROLLED ACCESS</p>
          <h1>统一评测，<br />让选择有据可依。</h1>
          <p className="login-intro">
            使用内部共享密码进入第三方 Agent 评测与排行榜平台。
          </p>
        </div>
        <div className="login-signal"><span />INTERNAL ONLY</div>
      </section>

      <section className="login-form-panel">
        <form className="login-form" onSubmit={submit}>
          <div className="login-form-heading">
            <span>01 / AUTHENTICATE</span>
            <h2>选择访问身份</h2>
          </div>

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

          <label className="password-field">
            <span>共享密码</span>
            <input
              type="password"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
                onResetError();
              }}
              placeholder="输入内部访问密码"
              required
              autoFocus
            />
          </label>

          <div className={`login-error ${error ? "is-visible" : ""}`} role="alert">
            {error ?? "占位"}
          </div>

          <button className="login-submit" type="submit" disabled={pending || !password}>
            <span>{pending ? "正在验证" : "进入平台"}</span>
            <span aria-hidden="true">-&gt;</span>
          </button>

          <p className="session-note">
            {role === "operator" ? "运营会话将在 8 小时后失效" : "只读会话将在 7 天后失效"}
          </p>
        </form>
      </section>
    </main>
  );
}
