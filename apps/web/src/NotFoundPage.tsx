import { Link } from "react-router-dom";

export function NotFoundPage({ operator }: { operator: boolean }) {
  return (
    <main className="workspace not-found-page">
      <span>404 / ROUTE NOT FOUND</span>
      <h1>这个页面不在当前工作区。</h1>
      <p>链接可能已失效，或者你没有访问该管理页面的权限。</p>
      <div><Link className="button" to={operator ? "/" : "/home"}>返回首页</Link><Link className="button is-ghost" to="/plaza">浏览 Agent 广场</Link></div>
    </main>
  );
}
