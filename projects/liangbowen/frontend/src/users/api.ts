import {
  type MutateOptions,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";

import { api } from "../shared/api/client";
import {
  userAccountPageSchema,
  userAccountSchema,
  type UserAccount,
} from "../shared/api/schemas";

const ACCOUNT_PAGE_SIZE = 50;

export const userKeys = {
  root: ["users", "accounts"] as const,
  actor: (actorId: string) => ["users", "accounts", actorId] as const,
  list: (actorId: string, offset: number) =>
    ["users", "accounts", actorId, ACCOUNT_PAGE_SIZE, offset] as const,
};

export interface UserCreatePayload {
  username: string;
  display_name: string;
  role: UserAccount["role"];
  password: string;
}

export type SafeUserCreateVariables = Omit<UserCreatePayload, "password">;

type UserCreateMutationOptions = MutateOptions<
  UserAccount,
  Error,
  SafeUserCreateVariables,
  unknown
>;

interface AccountCreationOperation {
  id: number;
  actorId: string;
}

function safeCreateVariables(payload: UserCreatePayload): SafeUserCreateVariables {
  return {
    username: payload.username,
    display_name: payload.display_name,
    role: payload.role,
  };
}

export function useUsers(actorId: string | undefined, offset: number) {
  return useQuery({
    queryKey: userKeys.list(actorId ?? "anonymous", offset),
    enabled: actorId !== undefined,
    retry: false,
    queryFn: ({ signal }) =>
      api.get("/users", userAccountPageSchema, {
        query: { limit: ACCOUNT_PAGE_SIZE, offset },
        signal,
      }),
  });
}

export function useCreateUser(actorId: string) {
  const queryClient = useQueryClient();
  const pendingPayloads = useRef(new Map<number, UserCreatePayload>());
  const nextOperationId = useRef(0);
  const mutation = useMutation<UserAccount, Error, AccountCreationOperation>({
    mutationKey: [...userKeys.actor(actorId), "create"],
    retry: false,
    mutationFn: (operation) => {
      const payload = pendingPayloads.current.get(operation.id);
      pendingPayloads.current.delete(operation.id);
      if (payload === undefined) throw new Error("Account creation payload is unavailable");
      return api.post("/users", payload, userAccountSchema);
    },
    onSuccess: (_created, operation) => {
      void queryClient.invalidateQueries({ queryKey: userKeys.actor(operation.actorId) });
    },
  });

  useEffect(() => () => pendingPayloads.current.clear(), []);

  const prepareOperation = useCallback((payload: UserCreatePayload): AccountCreationOperation => {
    nextOperationId.current += 1;
    const operation = { id: nextOperationId.current, actorId };
    pendingPayloads.current.set(operation.id, payload);
    return operation;
  }, [actorId]);

  const adaptOptions = useCallback(
    (payload: UserCreatePayload, options?: UserCreateMutationOptions) => {
      if (options === undefined) return undefined;
      const variables = safeCreateVariables(payload);
      return {
        onSuccess: (
          data: UserAccount,
          _operation: AccountCreationOperation,
          onMutateResult: unknown,
          context: Parameters<NonNullable<UserCreateMutationOptions["onSuccess"]>>[3],
        ) => options.onSuccess?.(data, variables, onMutateResult, context),
        onError: (
          error: Error,
          _operation: AccountCreationOperation,
          onMutateResult: unknown,
          context: Parameters<NonNullable<UserCreateMutationOptions["onError"]>>[3],
        ) => options.onError?.(error, variables, onMutateResult, context),
        onSettled: (
          data: UserAccount | undefined,
          error: Error | null,
          _operation: AccountCreationOperation,
          onMutateResult: unknown,
          context: Parameters<NonNullable<UserCreateMutationOptions["onSettled"]>>[4],
        ) => options.onSettled?.(data, error, variables, onMutateResult, context),
      };
    },
    [],
  );

  const mutate = useCallback(
    (payload: UserCreatePayload, options?: UserCreateMutationOptions) => {
      const operation = prepareOperation(payload);
      try {
        mutation.mutate(operation, adaptOptions(payload, options));
      } catch (error) {
        pendingPayloads.current.delete(operation.id);
        throw error;
      }
    },
    [adaptOptions, mutation, prepareOperation],
  );

  const mutateAsync = useCallback(
    (payload: UserCreatePayload, options?: UserCreateMutationOptions) => {
      const operation = prepareOperation(payload);
      try {
        return mutation.mutateAsync(operation, adaptOptions(payload, options));
      } catch (error) {
        pendingPayloads.current.delete(operation.id);
        throw error;
      }
    },
    [adaptOptions, mutation, prepareOperation],
  );

  return { ...mutation, mutate, mutateAsync };
}
