import multiprocessing
import sys
from threading import Thread
import click
import data_io
import pairing
import time
import datetime
import c_io_funcs

# Todo Find out if reads are being dropped


def process_template(read_template):
    c_io_funcs.sam_to_array(read_template)

    res = pairing.process(read_template)

    if res:
        read_template["passed"] = True
        c_io_funcs.add_scores(read_template, *res)
        c_io_funcs.choose_supplementary(read_template)
        c_io_funcs.score_alignments(read_template, read_template["ri"], read_template['rows'], read_template['data'])


def worker(queue, out_queue):
    while True:
        job = queue.get(True)

        if job == "Done":
            queue.task_done()
            out_queue.put("Done")
            break

        else:
            big_string = ""
            for data_tuple in job:
                read_template = data_io.make_template(*data_tuple)
                process_template(read_template)
                if read_template['passed']:
                    outstring = data_io.to_output(read_template)
                    if outstring:
                        big_string += outstring
                    else:
                        pass  # No mappings

            if len(big_string) > 0:
                out_queue.put(big_string)
            else:
                click.echo("WARNING: no output from job.", err=True)
        queue.task_done()


def process_reads(args):
    t0 = time.time()
    click.echo("fnfi reading data from {}".format(args["sam"]), err=True)
    click.echo("Replace hard clips == {}".format(args["replace_hardclips"] == "True"), err=True)
    if not args["include"]:
        args["bias"] = 1.0
    else:
        click.echo("Elevating alignments in --include with --bias {}".format(args["bias"]), err=True)

    if args["output"] == "-" or args["output"] is None:
        click.echo("Writing alignments to stdout", err=True)
        outsam = sys.stdout
    else:
        click.echo("Writing alignments to {}".format(args["output"]), err=True)
        outsam = open(args["output"], "w")

    count = 0

    # Use multiprocessing:
    # https://stackoverflow.com/questions/17241663/filling-a-queue-and-managing-multiprocessing-in-python
    if args["procs"] != 1:

        cpus = args["procs"] if args["procs"] != 0 else multiprocessing.cpu_count()
        click.echo("fnfi align runnning {} cpus".format(cpus), err=True)

        # Todo joinable queue is not efficient, other options?
        the_queue = multiprocessing.JoinableQueue(maxsize=cpus+2)
        out_queue = multiprocessing.Queue()

        the_pool = multiprocessing.Pool(args["procs"] if args["procs"] != 0 else multiprocessing.cpu_count(),
                                        worker, (the_queue, out_queue,))

        def writer_thread(q, outsam):
            while True:
                aln = q.get()
                if aln == "Done":
                    break
                elif aln == "Job failed":
                    click.echo("job failed", err=True)
                elif len(aln) > 1:
                    outsam.write(aln)

            click.echo("Writing done", err=True)

        writer = Thread(target=writer_thread, args=(out_queue, outsam, ))
        writer.setDaemon(True)
        writer.start()

        job = []
        itr = data_io.iterate_mappings(args)
        header_string = next(itr)
        out_queue.put(header_string)

        for data_tuple in itr:
            count += 1

            job.append(data_tuple)
            if len(job) > 500:
                the_queue.put(job)
                job = []
        if len(job) > 0:
            the_queue.put(job)

        the_queue.join()  # Wait for jobs to finish
        the_queue.put("Done")  # Send message to stop workers
        writer.join()  # Wait for writer to closing

    # Use single process for debugging
    else:
        click.echo("Single process", err=True)
        to_write = []  # Batch write

        itr = data_io.iterate_mappings(args)
        header_string = next(itr)
        to_write.append(header_string)

        for data_tuple in itr:

            count += 1
            temp = data_io.make_template(*data_tuple)
            process_template(temp)

            if temp['passed']:
                to_write.append(data_io.to_output(temp))

            if len(to_write) > 10000:  # Alignments to write
                for item in to_write:

                    if item is not None:
                        outsam.write(item)

                to_write = []

        for item in to_write:
            if item:
                outsam.write(item)

    if args["outsam"] != "-" or args["output"] is not None:
        outsam.close()

    click.echo("fnfi aln completed in {} h:m:s\n".format(str(datetime.timedelta(seconds=int(time.time() - t0)))),
               err=True)