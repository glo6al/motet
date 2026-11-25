**Name:** Review loop

**Use when:** A builder's work is ready for review, a review has come back, or a builder says a finding is fixed.

**Reads:** the branch and its commits, the review and its findings, and the checks before and after each fix.

### Steps

1. Check the builder got the other provider's review. Builders call it themselves, through its command line. If it didn't happen, ask the builder to run it. Where the project has a reviewer seat for extra checks, such as security, ask that reviewer too.
2. When Motet words a review request: what changed, where it is, what it is meant to do, and what to look at.
3. Send findings back to the builder exactly as found: the same words and the same severity. Don't soften, drop or add any. Say which ones block.
4. When the builder says it is fixed, check the fix landed: the commit is there, the finding is addressed, and the checks were run again, with their counts. Then ask for a review of the fix.
5. Repeat until the review is clean. If what is left needs the owner's decision, add it to Waiting on you.
6. If the same finding comes back after three rounds, stop the loop and bring it to the owner, with what each side says.

### Never

- Decide on its own that a finding doesn't matter.
- Call a review passed unless the reviewer said so.
- Merge, or ask anyone else to merge.

### For example

The review of Bram's export button finds two things: one blocks (the file includes another member's data in one case), one doesn't (a label). Motet sends both to Bram word for word, marks the first as blocking, and checks the next commit before asking for a second review.

This method grants no new authority. What Motet may do on its own is set by RULES.md and the permission table.
