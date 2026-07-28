import { describe, expect, it } from "vitest";

import {
  INITIAL_PASSWORD_MAX,
  INITIAL_PASSWORD_MIN,
  USERNAME_PATTERN,
  validateUserDraft,
} from "./userValidation";

const validDraft = {
  username: "student4",
  displayName: "学生丁",
  password: "Course2026!Secure",
  confirmPassword: "Course2026!Secure",
};

describe("account draft validation", () => {
  it("publishes the same deterministic username and password bounds as the server", () => {
    expect(USERNAME_PATTERN.test("student.4_test-user")).toBe(true);
    expect(USERNAME_PATTERN.test("Student4")).toBe(false);
    expect(INITIAL_PASSWORD_MIN).toBe(12);
    expect(INITIAL_PASSWORD_MAX).toBe(128);
  });

  it.each(["ab", "Student4", "student 4", "student/4", "学生4"])(
    "rejects non-canonical username %s",
    (username) => {
      expect(validateUserDraft({ ...validDraft, username }).username).toMatch(/3–64 位小写字母/u);
    },
  );

  it("accepts surrounding username whitespace for canonical trimming without lowercasing", () => {
    expect(validateUserDraft({ ...validDraft, username: "  student4\t" }).username).toBeUndefined();
    expect(validateUserDraft({ ...validDraft, username: "  Student4\t" }).username).toBeDefined();
  });

  it("accepts a trimmed visible display name and rejects invisible, long, or control text", () => {
    expect(validateUserDraft({ ...validDraft, displayName: "  学生丁  " }).displayName).toBeUndefined();
    expect(validateUserDraft({ ...validDraft, displayName: " \t " }).displayName).toBeDefined();
    expect(validateUserDraft({ ...validDraft, displayName: "学生\u0000丁" }).displayName).toBeDefined();
    expect(validateUserDraft({ ...validDraft, displayName: "学".repeat(129) }).displayName).toBeDefined();
  });

  it("enforces password length, trim, controls, username difference, and confirmation", () => {
    expect(validateUserDraft({ ...validDraft, password: "12345678901", confirmPassword: "12345678901" }).password).toBeDefined();
    expect(validateUserDraft({ ...validDraft, password: "x".repeat(129), confirmPassword: "x".repeat(129) }).password).toBeDefined();
    expect(validateUserDraft({ ...validDraft, password: " Course2026!Secure", confirmPassword: " Course2026!Secure" }).password).toBeDefined();
    expect(validateUserDraft({ ...validDraft, password: "Course2026!\u0000", confirmPassword: "Course2026!\u0000" }).password).toBeDefined();
    const twelveCharacters = "Abcdef123!xy";
    expect(validateUserDraft({ ...validDraft, password: twelveCharacters, confirmPassword: twelveCharacters }).password).toBeUndefined();
    expect(validateUserDraft({ ...validDraft, username: "student1234", password: "student1234", confirmPassword: "student1234" }).password).toBe("初始密码不能与用户名相同。");
    expect(validateUserDraft({ ...validDraft, confirmPassword: "different-password" }).confirmPassword).toBe("两次输入的密码不一致。");
    expect(validateUserDraft(validDraft)).toEqual({});
  });
});
