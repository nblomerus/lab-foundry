import { redirect } from "next/navigation";

// The floorplan is now the home page; keep /flow working as a redirect.
export default function FlowRedirect() {
  redirect("/");
}
