// Exercise the native linked-yard store offline against a synthetic yard home.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"yardr.dev/yardr/internal/store"
	"yardr.dev/yardr/internal/yardkey"
)

const operator = "pat"
const crew = "yardmaster"

var ctx = context.Background()

func must(err error) {
	if err != nil {
		panic(err)
	}
}

func addLink(st *store.Store, home, name, fixtureKey string, expose ...string) {
	key, _, err := yardkey.Ensure(filepath.Join(home, fixtureKey+".key"))
	must(err)
	_, enc, err := store.GenerateEnc()
	must(err)
	_, err = st.AddPeer(ctx, store.Peer{Name: name, Key: store.Public(key), Enc: enc}, operator)
	must(err)
	_, err = st.AllowPeer(ctx, name, store.PeerAllow{
		Send: []string{"mail", "file"}, Receive: []string{"mail"}, Expose: expose, MailLimit: 100,
	}, operator)
	must(err)
	must(st.PeerHeard(ctx, name, store.Public(key), store.Says("synthetic")))
}

func incoming(st *store.Store, peer, id, to, words string) {
	p, err := st.GetPeer(ctx, peer)
	must(err)
	body, err := json.Marshal(map[string]string{"from": "dispatch", "to": to, "text": words})
	must(err)
	taken, err := st.PeerReceive(ctx, peer, p.Key, []store.PeerMessage{{Kind: "mail", ID: id, Body: body}})
	must(err)
	if len(taken) != 1 || taken[0].Refused != "" {
		panic(fmt.Sprintf("synthetic mail refused: %+v", taken))
	}
}

func outgoing(st *store.Store, peer, from, words string, files ...store.BlobFile) *store.PeerOut {
	out, err := st.MailPeer(ctx, "dispatch", peer, words, from, files...)
	must(err)
	return out
}

func before(st *store.Store, home string) {
	for _, name := range []string{crew, "spare"} {
		_, err := st.PutCrew(ctx, store.CrewMember{Name: name, Runner: "herdr", Config: map[string]any{
			store.MailPeers: "north,south",
		}}, operator)
		must(err)
	}
	addLink(st, home, "north", "fixture-north", crew, "spare", operator)
	addLink(st, home, "south", "fixture-south", crew)
	incoming(st, "north", "before", crew, "Before activation")
	outgoing(st, "north", crew, "Old queued body")
}

func after(st *store.Store, home string) {
	old, err := st.PeerOutbox(ctx, "north", 0)
	must(err)
	if len(old) != 1 {
		panic("expected one old outbox entry")
	}
	must(st.PeerDelivered(ctx, "north", old[0].Seq))
	long := "Synthetic handoff notes\n\n" + strings.Repeat("Checklist: check <marker> & **keep** each line.\n", 102) + "\nEnd of handoff."
	blob, err := st.PutBlob(ctx, []byte("Synthetic attachment"))
	must(err)
	out := outgoing(st, "north", crew, long, store.BlobFile{Hash: blob.Hash, Name: "handoff.txt"})
	incoming(st, "north", out.ID, crew, "North: please review the synthetic handoff.")
	incoming(st, "south", out.ID, crew, "South: synthetic review complete.")
	incoming(st, "north", "not-listed", "spare", "Unlisted crew body")
	incoming(st, "north", "user", operator, "Human inbox body")
	must(st.Mail(ctx, crew, "Local mail body", operator))
	unlisted := outgoing(st, "north", operator, "Human outgoing body")
	must(st.PeerDelivered(ctx, "north", unlisted.Seq))
	failed := outgoing(st, "north", crew, "Failed outgoing body")
	must(st.PeerSendFailed(ctx, "north", failed.Seq, "synthetic refusal", time.Now(), true))
	if err := st.PeerDelivered(ctx, "north", failed.Seq); err == nil || !strings.Contains(err.Error(), "was given up") {
		panic("native store accepted a stale acknowledgement for a failed letter")
	}
	_, err = st.RenamePeer(ctx, "north", "company", operator)
	must(err)
	queued, err := st.PeerOutbox(ctx, "company", 0)
	must(err)
	for _, item := range queued {
		if item.Kind == "file" {
			must(st.PeerDelivered(ctx, "company", item.Seq))
		}
	}
	must(st.PeerDelivered(ctx, "company", out.Seq))
	dropped := outgoing(st, "south", crew, "Removed link body")
	must(st.DeletePeer(ctx, "south", operator))
	addLink(st, home, "south", "fixture-replacement", crew)
	if err := st.PeerDelivered(ctx, "south", dropped.Seq); !errors.Is(err, store.ErrNotFound) {
		panic("old acknowledgement reached replacement link")
	}
	incoming(st, "south", out.ID, crew, "Replacement: same wire ID, new linked yard.")
	must(st.LogDispatch(ctx, "", "human_notified", operator, map[string]any{"text": "Unrelated event body"}))
}

func main() {
	if len(os.Args) != 3 {
		panic("expected isolated home and fixture phase")
	}
	home, phase := os.Args[1], os.Args[2]
	st, err := store.Open(filepath.Join(home, "yard.db"))
	must(err)
	defer st.Close(ctx)
	switch phase {
	case "before":
		before(st, home)
	case "after":
		after(st, home)
	case "late":
		incoming(st, "company", "late", crew, "After restoring the operator binding.")
	case "reuse-crew":
		incoming(st, "company", "real-crew", crew, "Accepted by crew before its name was reused.")
		must(st.DeleteCrew(ctx, crew, "", operator))
		_, err := st.AddUser(ctx, crew, operator)
		must(err)
		_, err = st.AllowPeer(ctx, "company", store.PeerAllow{Expose: []string{crew}}, operator)
		must(err)
		incoming(st, "company", "replacement-user", crew, "Replacement human inbox body")
	case "failure":
		incoming(st, "company", "failure", crew, "Boundary recovery letter.\n"+strings.Repeat("abcd 🔹 & <x>\n", 800))
	case "conflict":
		incoming(st, "company", "conflict", "spare", "A newly included crew member.")
	default:
		panic("unknown synthetic fixture phase")
	}
}
