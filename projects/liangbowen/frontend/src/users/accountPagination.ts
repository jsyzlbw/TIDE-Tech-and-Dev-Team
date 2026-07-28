export const ACCOUNT_PAGE_SIZE = 50;
export const MAX_ACCOUNT_OFFSET = 10_000;

export function canPageForward(offset: number, total: number) {
  return offset < MAX_ACCOUNT_OFFSET && offset + ACCOUNT_PAGE_SIZE < total;
}
