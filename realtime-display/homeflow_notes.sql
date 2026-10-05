-- Additive schema. Run before the new API; legacy browser data is imported by API.
CREATE TABLE IF NOT EXISTS public.homeflow_notes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title text NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 240),
  position integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS homeflow_notes_order ON public.homeflow_notes(position, id);
CREATE TABLE IF NOT EXISTS public.homeflow_note_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  note_id uuid NOT NULL REFERENCES public.homeflow_notes(id) ON DELETE CASCADE,
  text text NOT NULL CHECK (length(trim(text)) BETWEEN 1 AND 2000),
  completed boolean NOT NULL DEFAULT false, position integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS homeflow_note_items_order ON public.homeflow_note_items(note_id, position, id);
CREATE TABLE IF NOT EXISTS public.homeflow_notes_imports (
  fingerprint text PRIMARY KEY, note_id uuid, imported_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.homeflow_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.homeflow_note_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.homeflow_notes_imports ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.homeflow_notes, public.homeflow_note_items, public.homeflow_notes_imports FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.homeflow_notes, public.homeflow_note_items, public.homeflow_notes_imports TO service_role;

CREATE OR REPLACE FUNCTION public.homeflow_notes_op(p_action text, p_id uuid DEFAULT NULL, p_data jsonb DEFAULT '{}'::jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
  result jsonb; parent_id uuid; new_id uuid; total_count integer; page_offset integer;
  page_limit integer := least(100, greatest(1, coalesce((p_data->>'limit')::integer, 100)));
BEGIN
  -- Serialize mutations across API workers, including migration deduplication/order.
  IF p_action NOT IN ('list', 'items') THEN PERFORM pg_advisory_xact_lock(184718, 1); END IF;
  IF p_action = 'list' THEN
    SELECT count(*) INTO total_count FROM public.homeflow_notes;
    page_offset := least(greatest(0,coalesce((p_data->>'offset')::integer,0)),greatest(0,total_count-1)) / page_limit * page_limit;
    SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.position,r.id),'[]'::jsonb) INTO result FROM (
      SELECT n.*, (SELECT count(*) FROM public.homeflow_note_items i WHERE i.note_id=n.id) AS item_count,
        (SELECT count(*) FROM public.homeflow_note_items i WHERE i.note_id=n.id AND i.completed) AS completed_count
      FROM public.homeflow_notes n ORDER BY n.position,n.id LIMIT page_limit OFFSET page_offset
    ) r;
    RETURN jsonb_build_object('notes',result,'total',total_count,'offset',page_offset,'limit',page_limit);
  ELSIF p_action = 'import' THEN
    SELECT note_id INTO new_id FROM public.homeflow_notes_imports WHERE fingerprint=p_data->>'fingerprint';
    IF FOUND OR jsonb_array_length(p_data->'items')=0 THEN
      RETURN jsonb_build_object('note_id',new_id,'imported',false);
    END IF;
    INSERT INTO public.homeflow_notes(title,position) SELECT 'Alınacaklar',coalesce(max(position),-1)+1 FROM public.homeflow_notes RETURNING id INTO new_id;
    INSERT INTO public.homeflow_note_items(note_id,text,position)
      SELECT new_id, value, ordinality-1 FROM jsonb_array_elements_text(p_data->'items') WITH ORDINALITY;
    INSERT INTO public.homeflow_notes_imports(fingerprint,note_id) VALUES(p_data->>'fingerprint',new_id);
    RETURN jsonb_build_object('note_id',new_id,'imported',true);
  ELSIF p_action = 'create_note' THEN
    INSERT INTO public.homeflow_notes(title,position) SELECT p_data->>'title',coalesce(max(position),-1)+1 FROM public.homeflow_notes RETURNING to_jsonb(homeflow_notes.*) INTO result;
    RETURN result;
  END IF;
  IF p_action IN ('update_item','delete_item') THEN
    SELECT note_id INTO parent_id FROM public.homeflow_note_items WHERE id=p_id FOR UPDATE;
  ELSE
    SELECT id INTO parent_id FROM public.homeflow_notes WHERE id=p_id FOR UPDATE;
  END IF;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','not_found'); END IF;
  IF p_action = 'items' THEN
    SELECT count(*) INTO total_count FROM public.homeflow_note_items WHERE note_id=p_id;
    page_offset := least(greatest(0,coalesce((p_data->>'offset')::integer,0)),greatest(0,total_count-1)) / page_limit * page_limit;
    SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.position,r.id),'[]'::jsonb) INTO result FROM (
      SELECT * FROM public.homeflow_note_items WHERE note_id=p_id ORDER BY position,id LIMIT page_limit OFFSET page_offset
    ) r;
    RETURN jsonb_build_object('note',(SELECT to_jsonb(n.*) FROM public.homeflow_notes n WHERE id=p_id),'items',result,'total',total_count,'offset',page_offset,'limit',page_limit);
  ELSIF p_action = 'update_note' THEN
    UPDATE public.homeflow_notes SET title=p_data->>'title',updated_at=now() WHERE id=p_id RETURNING to_jsonb(homeflow_notes.*) INTO result;
  ELSIF p_action = 'delete_note' THEN
    DELETE FROM public.homeflow_notes WHERE id=p_id;
    RETURN jsonb_build_object('id',p_id,'note_id',p_id,'deleted',true);
  ELSIF p_action = 'create_item' THEN
    INSERT INTO public.homeflow_note_items(note_id,text,position)
      SELECT p_id,p_data->>'text',coalesce(max(position),-1)+1 FROM public.homeflow_note_items WHERE note_id=p_id
      RETURNING to_jsonb(homeflow_note_items.*) INTO result;
  ELSIF p_action = 'update_item' THEN
    UPDATE public.homeflow_note_items SET text=coalesce(p_data->>'text',text),
      completed=coalesce((p_data->>'completed')::boolean,completed),updated_at=now() WHERE id=p_id
      RETURNING to_jsonb(homeflow_note_items.*) INTO result;
  ELSIF p_action = 'delete_item' THEN
    DELETE FROM public.homeflow_note_items WHERE id=p_id;
    result := jsonb_build_object('id',p_id,'note_id',parent_id,'deleted',true);
  ELSIF p_action = 'reorder' THEN
    IF (SELECT count(*) FROM public.homeflow_note_items WHERE note_id=p_id) <> jsonb_array_length(p_data->'ids')
      OR (SELECT count(DISTINCT value) FROM jsonb_array_elements_text(p_data->'ids')) <> jsonb_array_length(p_data->'ids')
      OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_data->'ids') r(value) WHERE NOT EXISTS (SELECT 1 FROM public.homeflow_note_items i WHERE i.id=r.value::uuid AND i.note_id=p_id)) THEN
      RETURN jsonb_build_object('error','order_conflict');
    END IF;
    UPDATE public.homeflow_note_items i SET position=r.ordinality-1,updated_at=now()
      FROM jsonb_array_elements_text(p_data->'ids') WITH ORDINALITY r(value,ordinality) WHERE i.id=r.value::uuid AND i.note_id=p_id;
    result := jsonb_build_object('note_id',p_id,'reordered',true);
  ELSE RETURN jsonb_build_object('error','invalid_action'); END IF;
  UPDATE public.homeflow_notes SET updated_at=now() WHERE id=parent_id;
  RETURN result;
END;
$$;
REVOKE ALL ON FUNCTION public.homeflow_notes_op(text,uuid,jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.homeflow_notes_op(text,uuid,jsonb) TO service_role;
NOTIFY pgrst, 'reload schema';
