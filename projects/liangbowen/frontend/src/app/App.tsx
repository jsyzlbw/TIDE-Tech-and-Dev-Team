import { useRef } from "react";
import { Outlet } from "react-router";

export function WorkspacePlaceholder() {
  return (
    <section className="workspace-placeholder" aria-labelledby="workspace-title">
      <p className="workspace-placeholder__folio" aria-hidden="true">
        WORKSPACE / 00
      </p>
      <div className="workspace-placeholder__copy">
        <p className="workspace-placeholder__kicker">编辑部案头 · EDITORIAL DESK</p>
        <h2 id="workspace-title">评阅，从一张清楚的案头开始。</h2>
        <p>
          作业、提交与教师审核将在这里按证据顺序展开。当前为产品壳层，业务工作区将在后续阶段接入。
        </p>
      </div>
      <p className="workspace-placeholder__note">基线 24 / 校阅状态待接入</p>
    </section>
  );
}

export function App() {
  const workspaceRef = useRef<HTMLElement>(null);
  return (
    <div className="app-frame">
      <a className="skip-link" href="#workspace" onClick={() => workspaceRef.current?.focus()}>
        跳到主要内容
      </a>

      <header className="masthead">
        <div className="masthead__rule" aria-hidden="true">
          <span>ACADEMIC REVIEW OFFICE</span>
          <span>EDITION 01 · 2026</span>
        </div>
        <div className="masthead__identity">
          <div className="masthead__index" aria-hidden="true">
            <span>01</span>
            <span>DS</span>
          </div>
          <div className="masthead__title">
            <p>TIDE CLUB · DATA STRUCTURES</p>
            <h1>作业评审台</h1>
          </div>
          <div className="masthead__registration" aria-label="系统状态">
            <span className="masthead__registration-label">SYSTEM REGISTER</span>
            <strong>LOCAL / READY</strong>
          </div>
        </div>
      </header>

      <main ref={workspaceRef} id="workspace" className="workspace" tabIndex={-1}>
        <Outlet />
      </main>

      <footer className="colophon">
        <span>AI ASSISTED · TEACHER VERIFIED</span>
        <span className="colophon__folio">TIDE / DS / 001</span>
      </footer>
    </div>
  );
}
