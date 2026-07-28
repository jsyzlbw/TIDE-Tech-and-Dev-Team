export const RUBRIC_POINT_LIMIT = 500;
export const RUBRIC_POINT_COUNT_LIMIT = 20;
export const GRADING_NOTES_LIMIT = 5_000;

export function codePointLength(value: string) {
  return [...value].length;
}

export function utf8ByteLength(value: string) {
  return new TextEncoder().encode(value).byteLength;
}
