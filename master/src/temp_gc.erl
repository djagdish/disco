-module(temp_gc).

-include_lib("kernel/include/file.hrl").

-export([start/1, start_link/1]).

-define(GC_INTERVAL, 600000).

start(Master) ->
    spawn_link(temp_gc, start_link, [Master]).

-spec start_link(node()) -> no_return().
start_link(Master) ->
    case catch register(temp_gc, self()) of
        {'EXIT', {badarg, _}} ->
            exit(already_started);
        _Else ->
            ok
    end,
    put(master, Master),
    loop().

-spec loop() -> no_return().
loop() ->
    case {get_purged(), get_jobs()} of
        {{ok, Purged}, {ok, Jobs}} ->
            case prim_file:list_dir(disco:data_root(node())) of
                {ok, Dirs} ->
                    Active = [Name || {Name, active, _Start, _Pid} <- Jobs],
                    process_dir(Dirs,
                                gb_sets:from_ordset(Purged),
                                gb_sets:from_list(Active));
                _Else ->
                    {retry, fresh_install}
            end;
        _Else ->
            {retry, master_busy}
    end,
    timer:sleep(?GC_INTERVAL),
    loop().

ddfs_delete(Tag) ->
    ddfs:delete({ddfs_master, get(master)}, Tag, internal).

get_purged() ->
    gen_server:call({disco_server, get(master)}, get_purged).

get_jobs() ->
    gen_server:call({event_server, get(master)}, get_jobs).

-spec process_dir([string()], gb_set(), gb_set()) -> 'ok'.
process_dir([], _Purged, _Active) -> ok;
process_dir([Dir|R], Purged, Active) ->
    Path = disco:data_path(node(), Dir),
    {ok, Jobs} = prim_file:list_dir(Path),
    [process_job(filename:join(Path, Job), Purged) ||
        Job <- Jobs, ifdead(Job, Active)],
    process_dir(R, Purged, Active).

-spec ifdead(string(), gb_set()) -> bool().
ifdead(Job, Active) ->
    not gb_sets:is_member(list_to_binary(Job), Active).

-spec process_job(string(), gb_set()) -> 'ok' | string().
process_job(JobPath, Purged) ->
    case prim_file:read_file_info(JobPath) of
        {ok, #file_info{type = directory, mtime = TStamp}} ->
            T = calendar:datetime_to_gregorian_seconds(TStamp),
            Now = calendar:datetime_to_gregorian_seconds(calendar:local_time()),
            Job = filename:basename(JobPath),
            IsPurged = gb_sets:is_member(list_to_binary(Job), Purged),
            GCAfter = list_to_integer(disco:get_setting("DISCO_GC_AFTER")),
            if IsPurged; Now - T > GCAfter ->
                    ddfs_delete(disco:oob_name(Job)),
                    os:cmd("rm -Rf " ++ JobPath);
               true ->
                    ok
            end;
        _ ->
            ok
    end.
