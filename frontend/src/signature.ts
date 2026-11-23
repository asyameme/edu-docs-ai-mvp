export type ProgramSignature = { programId: string; value: string };

export function signatureForProgram(signature: ProgramSignature | null, programId: string): string | null {
  return signature?.programId === programId ? signature.value : null;
}
