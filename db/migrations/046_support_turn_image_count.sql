-- How many images the turn was submitted with (spec
-- 2026-08-10-support-answer-images-design). The bytes themselves are not
-- stored: they ride in the RQ job payload like the doc attachment does.
--
-- The count is what makes the requeue degradation visible. requeue_support_turn
-- rebuilds SupportJobParams from this row and therefore carries no images, so a
-- requeued turn answers image-blind — on a ticket where the screenshots ARE the
-- question that is indistinguishable from a well-grounded answer unless someone
-- can see the original had images.
ALTER TABLE support_turns ADD COLUMN IF NOT EXISTS image_count INTEGER NOT NULL DEFAULT 0;
