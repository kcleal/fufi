"""
A basic assembler. Takes an overlap graph and merges reads in-place in a pileup style. Different soft-clipped regions
are then overlapped of 'linked'.
"""

import networkx as nx
import itertools
import difflib
import heapq
import numpy as np
from collections import deque
import click


def echo(*args):
    click.echo(args, err=True)


def to_prob(q):
    return pow(10, (-1*q/10))


def to_phred(p):
    return int(-10*np.log10(p + 1e-9))  # Ovoid overflow error


def update_edge(u, v, qual, G, kind, strand):
    if G.has_node(v):
        G.node[v]["w"] = to_phred(to_prob(G.node[v]["w"]) * to_prob(qual))  # A and B
        G.node[v]["n"] += 1
        G.node[v]["strand"] += strand
        if not G.has_edge(u, v):
            G.add_edge(u, v)
    else:
        G.add_node(v, w=qual, kind=kind, n=1, strand=strand, rid=[])
        G.add_edge(u, v)


def base_assemble(g, reads, bam, id):
    """
    Assembles reads that have overlaps. Uses alignment positions to determine contig construction
    :param g: The overlap graph
    :param reads: Dict of read_name: flag: alignment
    :param bam: Original bam for header access
    :param id: Unique ID for the event
    :return: Returns None if no soft-clipped portion of the cluster was assembled, otherwise a result dict is returned
    """
    # Note supplementary are included in assembly; helps link regions
    # Get reads of interest
    rd = [reads[n[0]][n[1]] for n in g.nodes()]

    G = nx.DiGraph()
    for r in rd:
        if r.seq is None:
            continue
        seq_pos = deque(list(zip(r.seq, r.get_reference_positions(full_length=True), r.query_qualities)))
        c_chrom = r.rname
        c_pos = r.pos
        strand = -1 if r.flag & 16 else 1
        pred = -1  # Start of sequence node
        v_first = None
        v_last = None
        for idx, (opp, length) in enumerate(r.cigartuples):

            if opp == 4:
                if idx == 0:  # Left clip
                    for j in range(length)[::-1]:
                        base, pos, qual = seq_pos.popleft()
                        offset = j + 1
                        u = pred
                        v = (base, c_chrom, c_pos, offset)
                        if not v_first:
                            v_first = v
                        update_edge(u, v, qual, G, "softclip_l", strand)
                        pred = v
                else:  # right clip
                    for j in range(length):
                        base, pos, qual = seq_pos.popleft()
                        offset = j + 1
                        u = pred
                        v = (base, c_chrom, c_pos, offset)
                        if j == length - 1:
                            v_last = v
                        update_edge(u, v, qual, G, "softclip_r", strand)
                        pred = v
            elif opp == 0 or opp == 7 or opp == 8:  # All match, match (=), mis-match (X)
                for j in range(length):
                    base, c_pos, qual = seq_pos.popleft()  # New c_pos defined
                    offset = 0
                    u = pred
                    v = (base, c_chrom, c_pos, offset)
                    if idx == 0:
                        v_first = v
                    elif idx == len(r.cigartuples) - 1 and j == length - 1:
                        v_last = v
                    update_edge(u, v, qual, G, "match", strand)
                    pred = v
            elif opp == 1:  # Insertion
                for j in range(length):
                    base, pos, qual = seq_pos.popleft()
                    offset = j + 1
                    u = pred
                    v = (base, c_chrom, c_pos, offset)
                    update_edge(u, v, qual, G, "insertion", strand)
                    pred = v
            elif opp == 5 or opp == 2:  # Hard clip or deletion
                continue
            elif opp == 3:
                break  # N

        # Add a start and end tag for each read, so reads contributing to contig sequence can be determined
        if v_first:
            G.node[v_first]["rid"].append((r.qname, r.flag))
        if v_last:
            G.node[v_last]["rid"].append((r.qname, r.flag))

    G.remove_node(-1)

    path = nx.algorithms.dag.dag_longest_path(G, weight="w")
    bases = []
    quals = []
    weights = []
    chroms = None
    sc_support_l, sc_support_r = 0, 0
    break_qual_l, break_qual_r = 0, 0
    strand_l, strand_r = 0, 0
    clipped = False
    ref_start, ref_end = 1e12, -1
    read_list = []

    for i in range(len(path)):
        ni = G.node[path[i]]
        weights.append(ni["w"])
        base, chrom, pos, offset = path[i]
        if ni["kind"] == "match":
            bases.append(base.upper())
        else:
            bases.append(base.lower())
        quals.append(254 if ni["w"] > 254 else ni["w"])  # Max qual is 254

        if ni["kind"] == "softclip_l":
            clipped = True
            if ni["n"] > sc_support_l:
                sc_support_l = ni["n"]
                break_qual_l = ni["w"] / float(ni["n"])
                strand_l = ni["strand"]
        if ni["kind"] == "softclip_r":
            clipped = True
            if ni["n"] > sc_support_r:
                sc_support_r = ni["n"]
                break_qual_r = ni["w"] / float(ni["n"])
                strand_r = ni["strand"]

        if pos < ref_start:
            ref_start = pos
        if pos > ref_end:
            ref_end = pos
        if not chroms:
            chroms = bam.get_reference_name(chrom)

        read_list += G.node[path[i]]["rid"]

    if clipped:

        res = {"bamrname": chroms,
                "qual_l": break_qual_l,
                "qual_r": break_qual_r,
                "base_quals": quals,
                "left_clips": sc_support_l,
                "right_clips": sc_support_r,
                "strand_l": strand_l,
                "strand_r": strand_r,
                "ref_start": ref_start,
                "ref_end": ref_end + 1,
                "read_names": set(read_list),
                "contig": "".join(bases),
                "id": id}

        return res


def rev_comp(s):
    d = {"A": "T", "C": "G", "T": "A", "G": "C", "N": "N", "|": "|", "a": "t", "t": "a", "c": "g", "g": "c",
         "n": "n"}
    return "".join(d[j] for j in s if j != "|")[::-1]


def explore_local(starting_nodes, large_component, color, upper_bound):
    seen = set(starting_nodes)
    found = set([])
    if len(starting_nodes) == 0:
        return set([])
    while True:
        nd = starting_nodes.pop()
        seen.add(nd)
        for edge in large_component.edges(nd, data=True):
            if edge[2]['c'] == color:
                if edge[0] not in seen:
                    starting_nodes.add(edge[0])
                    found.add(edge[0])
                elif edge[1] not in seen:
                    starting_nodes.add(edge[1])
                    found.add(edge[1])
            if len(found) > upper_bound:
                return set([])
        if len(starting_nodes) == 0:
            break
    return found


def linkup(assem, clip_length, large_component, insert_size, insert_stdev, read_length):
    """
    Takes assembled clusters and tries to link them together based on the number of spanning reads between clusters
    :param assem: A list of assembled clusters, each is a results dict
    :param clip_length: The input clip-length to call a 'good' overlap
    :return: Pairs of linked clusters, result dict is returned for each. The first-in-pair contains additional info.
    If no link was found for the cluster, a singleton is returned.
    """
    if len(assem) < 2:
        print("arrived linkup in cluster; help")
        quit()
        for i in assem:
            j = i.copy()
            j["linked"] = False
            j_tup = (5 if j["left_clips"] > j["right_clips"] else 3,
                     j["read_names"],
                     j["bamrname"],
                     j["ref_start"] if j["left_clips"] > j["right_clips"] else j["ref_end"])
            call_info, contributing_reads = call_break_points([j_tup])
        return [[j, None, call_info, contributing_reads]]  # Return singletons

    # Add supplementary edges to assembled clusters (yellow edges)
    for i in range(len(assem)):
        # Search local edges
        link_nodes = assem[i]["read_names"]
        assem[i]["link_nodes"] = link_nodes.union(explore_local(link_nodes.copy(),
                                                                large_component, "y", 2*len(link_nodes)))

    # Decide which sequences to overlap based on the number of reads shared between clusters
    shared_templates_heap = []
    for a, b in itertools.combinations(assem, 2):

        common = len(set([j[0] for j in a["link_nodes"]]).intersection(set([j[0] for j in b["link_nodes"]])))

        # Expected local nodes
        # Read clouds uncover variation in complex regions of the human genome. Bishara et al 2016.
        # max template size * estimated coverage / read_length; 2 just to increase the upper bound a bit
        local_upper_bound = ((insert_size + (2*insert_stdev)) * float(2 + common)) / float(read_length)

        # Explore region a for grey edges
        a_local = explore_local(a["link_nodes"].copy(), large_component, "g", local_upper_bound)
        b_local = explore_local(b["link_nodes"].copy(), large_component, "g", local_upper_bound)

        if len(a_local) > 0 and len(b_local) > 0:
            common += len(set([j[0] for j in a_local]).intersection(set([j[0] for j in b_local])))

        a["intersection"] = common
        if common > 0:
            item = (-1*common, (a, b))
            # Todo change to some other priority queue, or try heapify; heappush seems to bug out with lots of same val
            heapq.heappush(shared_templates_heap, item)  # Max heapq
            print(common, len(a), len(b), item[0])
            print(a)
            print(b)

    seen = set([])
    results = []
    paired = set([])

    for _ in range(len(shared_templates_heap)):

        try:
            n_common, pair = heapq.heappop(shared_templates_heap)
        except:
            print(len(shared_templates_heap))
            for item in shared_templates_heap:
                print(item)
            quit()

        if n_common == 0:  # No reads in common, no pairing
            continue

        a, b = pair
        if a["id"] in seen or b["id"] in seen:
            seen.add(a["id"])
            seen.add(b["id"])
            continue  # A or B has already been paired with a higher connectivity cluster
        seen.add(a["id"])
        seen.add(b["id"])

        seqs = []
        for i in (a, b):
            left_clipped = False
            negative_strand = True if i["strand_r"] < 0 else False
            sc_support = i["right_clips"]
            qual = int(i["qual_r"])

            if i["left_clips"] > i["right_clips"]:
                left_clipped = True
                negative_strand = True if i["strand_l"] < 0 else False
                sc_support = i["left_clips"]
                qual = int(i["qual_l"])

            seqs.append((i["contig"], sc_support, i["bamrname"], i["ref_start"], i["ref_end"] + 1, left_clipped,
                         negative_strand, qual))

        ainfo, binfo = seqs
        aseq, bseq = ainfo[0], binfo[0]
        # If seqs are on the same chrom
        if ainfo[2] == binfo[2]:
            # If clips are on same side, rev comp one of them
            if ainfo[5] == binfo[5]:
                bseq = rev_comp(bseq)
        else:
            if ainfo[6]:  # negative strand
                aseq = rev_comp(aseq)
            if binfo[6]:
                bseq = rev_comp(bseq)

        # See https://docs.python.org/2/library/difflib.html
        m = difflib.SequenceMatcher(a=aseq.upper(), b=bseq.upper(), autojunk=None)
        longest = m.find_longest_match(0, len(aseq), 0, len(bseq))

        a_align = [i.islower() for i in aseq[longest[0]:longest[0] + longest[2]]]
        b_align = [i.islower() for i in bseq[longest[1]:longest[1] + longest[2]]]

        sc_a = sum(a_align)
        sc_b = sum(b_align)
        # non_sc_a = len(a_align) - sc_a
        # non_sc_b = len(b_align) - sc_b

        # Add some breakpoint info, even if pair can not be linked
        # Need (3 or 5 join, soft-clip length, chromosome, break point position)
        a_tup = (5 if a["left_clips"] > a["right_clips"] else 3,
                 a["read_names"],
                 a["bamrname"],
                 a["ref_start"] if a["left_clips"] > a["right_clips"] else a["ref_end"])
        b_tup = (5 if b["left_clips"] > b["right_clips"] else 3,
                 b["read_names"],
                 b["bamrname"],
                 b["ref_start"] if b["left_clips"] > b["right_clips"] else b["ref_end"])
        # Todo remove call_break_points from this script, invoke this later
        # call_info, contributing_reads = call_break_points([a_tup, b_tup])
        best_sc = max([sc_a, sc_b])
        # best_non_sc = max([non_sc_a, non_sc_b])
        a2 = a.copy()
        b2 = b.copy()
        a2["linked"] = False

        if best_sc > clip_length:  # and best_non_sc >= 5:
            a2["linked"] = True
            paired.add(a["id"])
            paired.add(b["id"])
        results.append([a2, b2])  #, call_info, contributing_reads])

    # Deal with un-linked clusters
    for a in assem:
        if a["id"] not in paired:
            a2 = a.copy()
            a2["linked"] = False
            a_tup = (5 if a["left_clips"] > a["right_clips"] else 3,
                     a["read_names"],
                     a["bamrname"],
                     a["ref_start"] if a["left_clips"] > a["right_clips"] else a["ref_end"])
            results.append([a_tup])
            # call_info, contributing_reads = call_break_points([a_tup])
            # results.append([a2, None, call_info, contributing_reads])

    return results


def merge_assemble(grp, all_reads, bam, clip_length, insert_size, insert_stdev, read_length):
    """
    Takes an overlap graph and breaks its down into clusters with overlapping soft-clips. Contig sequences are generated
    for each of these clusters. Then contigs are paired up and linked. If no linking can be found the contig is treated
    as a singleton.
    :param grp:
    :param all_reads:
    :param bam:
    :param clip_length:
    :return:
    """
    edges = grp.edges(data=True)

    gray = [i for i in edges if i[2]['c'] == "g"]
    yellow = [i for i in edges if i[2]['c'] == "y"]
    black = [i for i in edges if i[2]['c'] == "b"]

    # First identify clusters of reads with overlapping-soft clips i.e. sub graphs with black edges
    sub_grp = nx.Graph()
    sub_grp.add_edges_from([i for i in edges if i[2]["c"] == "b"])  # black edges = matching reads with soft-clips
    sub_grp_cc = list(nx.connected_component_subgraphs(sub_grp))
    sub_clusters = len(sub_grp_cc)

    look_for_secondary = True  # If nothing can be assembled skip to look for secondary evidence (grey edges)
    if sub_clusters > 0:

        assembled = [base_assemble(i, all_reads, bam, idx) for idx, i in enumerate(sub_grp_cc)]
        linkedup = linkup(assembled, clip_length, grp, insert_size, insert_stdev, read_length)

        if len(linkedup) > 0:

            for item in linkedup:
                if len(item) == 2:  # Contig for each side?

            #for (side1, side2, call_info, contr_reads) in linkedup:  # Possibility of multiple events
                if len(gray) == 0 and len(yellow) == 0:
                    # Could call a single break end here BND
                    continue
                look_for_secondary = False
                call_result = {}
                read_set = contr_reads

                if side1["linked"]:
                    call_result.update(call_info)  # Use the call information from the contig assembly if liked
                    call_result["contig"] = side1["contig"].upper() if len(side1["contig"]) >= len(side2["contig"]) else side2["contig"]
                    call_result["PRECISE"] = True
                    call_result["linked"] = True
                    call_result["linked_clip_support"] = call_info["nreads"]
                    # Todo score_reads outside of this script
                    call_result.update(score_reads(read_set, all_reads))
                    echo(call_result)
                    yield call_result

    #
    #             else:
    #
    #                 continue
    #                 call_result["contig"] = side1["contig"]
    #                 # if len(yellow) > 0:
    #                 #     call_y, contributing_reads = process_edge_set(yellow, all_reads, bam, insert_size, insert_stdev, get_mate=False)
    #                 #     call_result["supp_support"] = call_y["nreads"]
    #                 #     if call_y["cipos95A"] == 0:
    #                 #         call_result["PRECISE"] = True
    #                 #         call_result.update(call_y)  # Use this as the main call
    #                 #     read_set.union(contributing_reads)
    #
    #                 # if len(black) > 0:
    #                 #     call_b, contributing_reads = process_edge_set(black, all_reads, bam, insert_size, insert_stdev)
    #                 #     call_result["clip_support"] = call_b["nreads"]
    #                 #
    #                 #     if call_info["cipos95A"] == 0 and len(call_result) < 3:
    #                 #         call_result["PRECISE"] = True
    #                 #         call_result.update(call_info)
    #                 #         call_result["clip_support"] = call_b["nreads"]
    #                 #     read_set.union(contributing_reads)
    #
    #                 if len(gray) > 0:
    #                     call_g, contributing_reads = process_edge_set(gray, all_reads, bam, insert_size, insert_stdev)
    #                     call_result["pe_support"] = call_g["nreads"]
    #                     if len(call_result) < 5:
    #                         call_result.update(call_g)
    #                     read_set.union(contributing_reads)
    #                 call_result["linked"] = False
    #                 call_result.update(score_reads(read_set, all_reads))
    #                 yield call_result
    #     # else:
    #     #     print("len linked up is 0")
    # return
    # if look_for_secondary:
    #
    #     read_set = set([])
    #     # No black edges
    #     # Look for other evidence
    #     call_result = {}
    #     if len(yellow) > 0:
    #         call_info, contributing_reads = process_edge_set(yellow, all_reads, bam, insert_size, insert_stdev, get_mate=False)
    #         call_result.update(call_info)
    #         call_result["supp_support"] = call_info["nreads"]
    #         if call_info["cipos95A"] == 0:
    #             call_result["PRECISE"] = True
    #         else:
    #             call_result["PRECISE"] = False
    #         read_set.union(contributing_reads)
    #
    #     if len(black) > 0:
    #         call_info, contributing_reads = process_edge_set(black, all_reads, bam, insert_size, insert_stdev)
    #         if len(call_result) == 0:
    #             call_result.update(call_info)
    #         call_result["clip_support"] = call_info["nreads"]
    #         call_result["PRECISE"] = False
    #         read_set.union(contributing_reads)
    #
    #     if len(gray) > 0:
    #         call_info, contributing_reads = process_edge_set(gray, all_reads, bam, insert_size, insert_stdev)
    #         if len(call_result) == 0:
    #             call_result.update(call_info)
    #         call_result["pe_support"] = call_info["nreads"]
    #         call_result["PRECISE"] = False
    #         read_set.union(contributing_reads)
    #
    #     if len(call_result) > 0:
    #         call_result.update(score_reads(read_set, all_reads))
    #         yield call_result