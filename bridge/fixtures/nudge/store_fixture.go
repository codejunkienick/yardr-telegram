// Offline synthetic daemon writes for the nudge card; never part of the candidate core binary.
package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"yardr.dev/yardr/internal/store"
)

const boot = "synthetic-daemon-boot"

func main() {
	if len(os.Args) < 4 {
		panic("expected isolated home, boundary, crew member")
	}
	home, boundary, crew := os.Args[1], os.Args[2], os.Args[3]
	ctx := context.Background()
	st, err := store.Open(filepath.Join(home, "yard.db"))
	must(err)
	defer st.Close(ctx)
	switch boundary {
	case "file":
		// As the daemon raises session_harness_error for a crew member: the error on its handle, then its card.
		handle := map[string]any{"agent": "synthetic-pane", store.HandleKind: "claude"}
		member, err := st.GetCrew(ctx, crew)
		must(err)
		if !member.CrewSession.Live() {
			must(st.CrewStartedFor(ctx, crew, "", handle))
		}
		handle["harness_error"] = "network"
		must(st.SetCrewHandleFor(ctx, crew, "", handle))
		seq, err := st.AddAsk(ctx, member.Journal, store.Ask{
			Question: "crew member " + crew + "'s harness stopped on network: nudge it to go on?",
			To:       os.Args[4],
			Context: []string{"The error line: synthetic network error", "The harness: claude",
				"Seen at " + time.Now().Format(time.RFC3339)},
			Run: &store.AskRun{Argv: []string{"session", "nudge", crew}},
		}, "yard")
		must(err)
		fmt.Println(member.Journal, seq)
	case "start":
		// As the yard starts a crew member's line session, with no ask: a
		// typed nudge's live target, filed by no one.
		handle := map[string]any{"agent": "synthetic-pane", store.HandleKind: "claude"}
		member, err := st.GetCrew(ctx, crew)
		must(err)
		if !member.CrewSession.Live() {
			must(st.CrewStartedFor(ctx, crew, "", handle))
		}
		fmt.Println(member.Journal)
	case "finish":
		// As the daemon finishes an approved request: claimed under its boot, then its outcome.
		member, err := st.GetCrew(ctx, crew)
		must(err)
		if member.CrewSession.Nudge == nil {
			panic("no nudge requested")
		}
		id := member.CrewSession.Nudge.ID
		must(st.ClaimNudge(ctx, id, boot))
		data := map[string]any{}
		if os.Args[5] != "" {
			data["reason"] = os.Args[5]
		}
		must(st.FinishNudge(ctx, id, boot, os.Args[4], data))
	case "takeback":
		member, err := st.GetCrew(ctx, crew)
		must(err)
		seq, err := strconv.ParseUint(os.Args[4], 10, 64)
		must(err)
		must(st.TakeBackAsk(ctx, member.Journal, seq, "the harness error of "+crew+" is over", "yard"))
	case "file-conversation":
		// As the daemon raises session_harness_error for a crew conversation:
		// the conversation's session, then its card naming the conversation.
		user := "pat"
		handle := map[string]any{"agent": "synthetic-pane", store.HandleKind: "claude"}
		member, err := st.GetCrew(ctx, crew)
		must(err)
		if !member.CrewSession.Live() {
			must(st.CrewStartedFor(ctx, crew, "", handle))
			member, err = st.GetCrew(ctx, crew)
			must(err)
		}
		// A conversation session reads where the crew serves the user with
		// its env key set.
		env := filepath.Join(home, "synthetic.env")
		must(os.WriteFile(env, []byte("SYNTHETIC=1\n"), 0600))
		member.Config[store.EnvForPrefix+user] = env
		member, err = st.PutCrew(ctx, *member, "pat")
		must(err)
		if !member.Session(user).Live() {
			must(st.CrewStartedFor(ctx, crew, user, handle))
			member, err = st.GetCrew(ctx, crew)
			must(err)
		}
		conversation := map[string]any{"agent": "synthetic-pane", store.HandleKind: "claude"}
		conversation["harness_error"] = "network"
		must(st.SetCrewHandleFor(ctx, crew, user, conversation))
		seq, err := st.AddAsk(ctx, member.Journal, store.Ask{
			Question: "crew member " + crew + "'s conversation for " + user + " stopped on network: nudge it to go on?",
			To:       os.Args[4],
			Context: []string{"The error line: synthetic network error", "The harness: claude",
				"Seen at " + time.Now().Format(time.RFC3339)},
			Run: &store.AskRun{Argv: []string{"session", "nudge", crew, "--for", user}},
		}, "yard")
		must(err)
		fmt.Println(member.Journal, seq)
	case "status-conversation":
		// Differing runtime statuses for the confirm card: the line idles
		// while the conversation for pat is blocked.
		must(st.CrewStatusFor(ctx, crew, "", "idle"))
		must(st.CrewStatusFor(ctx, crew, "pat", "blocked"))
	case "seed-session":
		// As the yard claims a bead's session on a group with a kind but no
		// model: ListSessions reads it with no model key.
		must(st.PutGroup(ctx, store.Group{Name: "synthetic-workers", Runner: "herdr",
			Config: map[string]any{"kind": "claude"}, Limit: 1}, "pat"))
		bead, err := st.CreateBead(ctx, store.NewBead{Title: "Synthetic default model session",
			Stage: "new", Type: "task"}, "pat")
		must(err)
		claimed, err := st.Claim(ctx, bead.ID, "synthetic-workers", "new", "yard")
		must(err)
		must(st.MarkRunning(ctx, claimed.ID,
			map[string]any{"agent": "synthetic-pane", store.HandleKind: "claude"}))
		fmt.Println(claimed.ID, bead.ID)
	case "finish-queued":
		// As the daemon finishes a request typed into a working agent: the
		// outcome carries working and queued.
		member, err := st.GetCrew(ctx, crew)
		must(err)
		if member.CrewSession.Nudge == nil {
			panic("no nudge requested")
		}
		id := member.CrewSession.Nudge.ID
		must(st.ClaimNudge(ctx, id, boot))
		must(st.FinishNudge(ctx, id, boot, "typed", map[string]any{"working": true, "queued": true}))
	case "finish-conversation":
		// As the daemon finishes a conversation's approved request.
		member, err := st.GetCrew(ctx, crew)
		must(err)
		if member.Session("pat").Nudge == nil {
			panic("no nudge requested")
		}
		id := member.Session("pat").Nudge.ID
		must(st.ClaimNudge(ctx, id, boot))
		data := map[string]any{}
		if os.Args[5] != "" {
			data["reason"] = os.Args[5]
		}
		must(st.FinishNudge(ctx, id, boot, os.Args[4], data))
	default:
		panic("unknown synthetic boundary")
	}
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
