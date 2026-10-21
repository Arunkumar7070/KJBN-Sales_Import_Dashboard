import { Link } from "react-router-dom";
import { Card, EmptyState, primaryButton } from "../components/ui";

export default function NotFoundPage() {
  return (
    <Card className="p-6">
      <EmptyState
        icon="explore_off"
        title="Page not found"
        hint="This address doesn't match any section of the console."
        action={
          <Link to="/" className={`${primaryButton} mt-2`}>
            Go to Analytics
          </Link>
        }
      />
    </Card>
  );
}
