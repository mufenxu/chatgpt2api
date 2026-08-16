"use client";

import { login } from "@/lib/api";
import { clearStoredAuthSession, getStoredAuthSession, setStoredAuthSession, type StoredAuthSession } from "@/store/auth";

export async function getValidatedAuthSession(): Promise<StoredAuthSession | null> {
  const storedSession = await getStoredAuthSession();

  try {
    const data = await login();
    const nextSession: StoredAuthSession = {
      key: "",
      authType: "my",
      role: data.role,
      subjectId: data.subject_id,
      name: data.name,
    };
    await setStoredAuthSession(nextSession);
    return nextSession;
  } catch {
    if (!storedSession || storedSession.authType !== "key") {
      await clearStoredAuthSession();
      return null;
    }
  }

  try {
    const data = await login(storedSession.key);
    const nextSession: StoredAuthSession = {
      key: storedSession.key,
      authType: "key",
      role: data.role,
      subjectId: data.subject_id,
      name: data.name,
    };
    await setStoredAuthSession(nextSession);
    return nextSession;
  } catch {
    await clearStoredAuthSession();
    return null;
  }
}
