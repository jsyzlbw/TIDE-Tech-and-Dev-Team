import type { FormEvent } from "react";

export type AnswerMode = "text" | "markdown" | "code";

interface AnswerEditorProps {
  mode: AnswerMode;
  text: string;
  dirty: boolean;
  submittable: boolean;
  submitting: boolean;
  onModeChange(mode: AnswerMode): void;
  onTextChange(text: string): void;
  onDiscard(): void;
  onSubmit(): void;
}

export function AnswerEditor({
  mode,
  text,
  dirty,
  submittable,
  submitting,
  onModeChange,
  onTextChange,
  onDiscard,
  onSubmit,
}: AnswerEditorProps) {
  const characterCount = [...text].length;
  const formattedCharacterCount = new Intl.NumberFormat("zh-CN").format(characterCount);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit();
  }

  return (
    <form className="answer-editor" onSubmit={submit}>
      <div className="answer-editor__header">
        <div>
          <p className="student-kicker">NEW SUBMISSION</p>
          <h3>编辑答案</h3>
        </div>
        <span className={dirty ? "draft-mark draft-mark--dirty" : "draft-mark"}>
          {dirty ? "有未提交修改" : "草稿已同步"}
        </span>
      </div>
      <label className="answer-editor__mode">
        <span>答案格式</span>
        <select name="answer_mode" autoComplete="off" value={mode} onChange={(event) => onModeChange(event.target.value as AnswerMode)}>
          <option value="text">纯文本</option>
          <option value="markdown">Markdown</option>
          <option value="code">代码</option>
        </select>
      </label>
      <label className="answer-editor__text">
        <span>作业答案</span>
        <textarea
          name="answer_content"
          autoComplete="off"
          inputMode="text"
          value={text}
          rows={14}
          spellCheck={mode !== "code"}
          onChange={(event) => onTextChange(event.target.value)}
        />
      </label>
      <div className="answer-editor__footer">
        <span className={characterCount > 50_000 ? "character-count character-count--error" : "character-count"}>
          {formattedCharacterCount} / 50,000 字符
        </span>
        <div>
          <button className="button button--secondary" type="button" disabled={!dirty || submitting} onClick={onDiscard}>放弃草稿</button>
          <button className="button" type="submit" disabled={!submittable || submitting}>
            {submitting ? "正在提交…" : "提交答案"}
          </button>
        </div>
      </div>
      {!submittable ? <p className="answer-editor__closed">当前作业不可提交；你仍可以查看既有版本与报告。</p> : null}
    </form>
  );
}
