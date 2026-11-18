// Offline synthetic store boundaries; never part of the candidate core binary.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"time"

	bolt "go.etcd.io/bbolt"
	"yardr.dev/yardr/internal/store"
)

func main() {
	if len(os.Args) != 5 {
		panic("expected isolated home, boundary, bead, ask")
	}
	home, boundary, bead := os.Args[1], os.Args[2], os.Args[3]
	seq, err := strconv.ParseUint(os.Args[4], 10, 64)
	must(err)
	path := filepath.Join(home, "yard.db")
	if boundary == "expire" {
		db, err := bolt.Open(path, 0600, &bolt.Options{Timeout: time.Second})
		must(err)
		defer db.Close()
		must(db.Update(func(tx *bolt.Tx) error {
			notes := tx.Bucket([]byte("notes"))
			found := false
			err := notes.ForEach(func(key, value []byte) error {
				var note store.Note
				if err := json.Unmarshal(value, &note); err != nil {
					return err
				}
				if note.Seq != seq || note.Kind != "ask" {
					return nil
				}
				ask, err := store.ParseAsk(note)
				if err != nil {
					return err
				}
				if ask.Run == nil || ask.Run.General == nil {
					return fmt.Errorf("expected general ask")
				}
				expiry := time.Now().Add(-2 * time.Hour)
				ask.Run.Seen["general expires"] = expiry.UTC().Format(time.RFC3339)
				ask.Run.Does[2] = "It is good until " + expiry.Local().Format("15:04:05") + "."
				raw, err := json.Marshal(ask)
				if err != nil {
					return err
				}
				note.Text = string(raw)
				raw, err = json.Marshal(note)
				if err != nil {
					return err
				}
				found = true
				return notes.Put(key, raw)
			})
			if err != nil {
				return err
			}
			if !found {
				return fmt.Errorf("synthetic ask not found")
			}
			return nil
		}))
		return
	}
	if boundary != "claim" {
		panic("unknown synthetic boundary")
	}
	st, err := store.Open(path)
	must(err)
	defer st.Close(context.Background())
	_, err = st.ApproveAsk(context.Background(), bead, seq, "pat", true, nil)
	must(err)
	rec, err := st.ClaimCarry(context.Background(), "synthetic-prior-boot", time.Now())
	must(err)
	if rec == nil || !rec.General || rec.Ask != seq {
		panic("unexpected claimed carry")
	}
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
