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
	default:
		panic("unknown synthetic boundary")
	}
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
