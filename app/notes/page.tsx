const notes = [
  {
    slug: "server-components",
    title: "Server components by default",
    body: "Everything under app/ is a server component unless it opts into 'use client'. Props crossing the boundary must be serializable.",
  },
  {
    slug: "layouts",
    title: "Layouts nest and persist",
    body: "Layouts keep state across navigation within their subtree. Good place for navs, bad place for per-page data.",
  },
  {
    slug: "route-handlers",
    title: "Route handlers",
    body: "app/api/*/route.ts replaces pages/api. Export GET/POST functions returning Response objects.",
  },
];

export default function NotesPage() {
  return (
    <div>
      <h1>Notes</h1>
      <p>Things I keep forgetting.</p>
      {notes.map((note) => (
        <section key={note.slug}>
          <h2>{note.title}</h2>
          <p>{note.body}</p>
        </section>
      ))}
    </div>
  );
}
