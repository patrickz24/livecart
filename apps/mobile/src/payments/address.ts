import type { ShippingAddress } from "@livecart/api-client";

/** The address form's fields, as typed. */
export interface AddressFields {
  full_name: string;
  line1: string;
  line2: string;
  postal_code: string;
  city: string;
  country: string;
}

export type AddressProblems = Partial<Record<keyof AddressFields, string>>;

export const EMPTY_ADDRESS: AddressFields = {
  full_name: "",
  line1: "",
  line2: "",
  postal_code: "",
  city: "",
  country: "FR",
};

export function fieldsOf(address: ShippingAddress | null): AddressFields {
  return address === null ? EMPTY_ADDRESS : { ...address, line2: address.line2 ?? "" };
}

// The API's bounds (ShippingAddress in livecart_core.application.dto), checked
// here first so that the buyer sees which field to fix; the API checks again.
const LENGTHS = [
  ["full_name", 1, 100],
  ["line1", 1, 200],
  ["line2", 0, 200],
  ["postal_code", 1, 16],
  ["city", 1, 100],
] as const;

/** The address to send, trimmed, or the problems of its fields. */
export function validateAddress(
  fields: AddressFields,
): { address: ShippingAddress; problems: null } | { address: null; problems: AddressProblems } {
  const address: AddressFields = {
    full_name: fields.full_name.trim(),
    line1: fields.line1.trim(),
    line2: fields.line2.trim(),
    postal_code: fields.postal_code.trim(),
    city: fields.city.trim(),
    country: fields.country.trim().toUpperCase(),
  };
  const problems: AddressProblems = {};
  for (const [name, min, max] of LENGTHS) {
    const { length } = address[name];
    if (length < min) {
      problems[name] = "Required.";
    } else if (length > max) {
      problems[name] = `${String(max)} characters at most.`;
    }
  }
  if (!/^[A-Z]{2}$/.test(address.country)) {
    problems.country = "Two letters, such as FR.";
  }
  return Object.keys(problems).length > 0
    ? { address: null, problems }
    : { address, problems: null };
}
