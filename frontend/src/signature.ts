export type ProgramSignature = { programId: string; value: string };

type SignatureResult =
  | { status: "accepted"; signature: ProgramSignature }
  | { status: "cancelled" | "invalid" };

export function signatureForProgram(signature: ProgramSignature | null, programId: string): string | null {
  return signature?.programId === programId ? signature.value : null;
}

export function requestSignature(
  program: { id: string; name: string },
  previous: ProgramSignature | null = null,
): SignatureResult {
  const entered = window.prompt(
    `Введите имя для подписи в программе «${program.name}» (до 120 символов):`,
    signatureForProgram(previous, program.id) ?? "",
  );
  if (entered === null) return { status: "cancelled" };
  const value = entered.trim();
  if (!value || Array.from(value).length > 120) return { status: "invalid" };
  return { status: "accepted", signature: { programId: program.id, value } };
}
